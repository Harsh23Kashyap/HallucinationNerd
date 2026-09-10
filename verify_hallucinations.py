"""
Hallucination Verification Engine (HVE)
========================================

Validate an answer against its references — per-claim verdicts with confidence.

Takes the question, the answer, and the references the answer is grounded in.
Splits the answer into atomic claims, finds the most relevant spans in the
references for each claim, verifies each (claim, reference) pair, and returns
a per-claim verdict with confidence plus an overall reliability assessment.

Usage:
    python verify_hallucinations.py --input <dataset.json> --output-dir results/
    python verify_hallucinations.py --input <dataset.json> --task citation
    python verify_hallucinations.py --input <dataset.json> --task evaluation

Input format:
    [
        {
            "question_id": "Q1",
            "question": "What was asked",
            "synopsis": "The answer to verify, with [1] citations",
            "retrieved_articles": [
                {
                    "id": "...",
                    "title": "...",
                    "content": "...",  # full text (or use 'url'/'path' for auto-parsing)
                    "url": "...",      # optional: web page URL to fetch
                    "path": "..."      # optional: local file path (PDF, txt, etc.)
                }
            ]
        }
    ]

Supports references as:
    - Pre-extracted text (in 'content' field)
    - PDF files (via 'path' field — auto-extracted)
    - Web pages (via 'url' field — auto-fetched)
    - Plain text files (via 'path' field)

Requires: openai>=1.0.0, python-dotenv
Optional: PyMuPDF (for PDF), requests+beautifulsoup4 (for web pages)
"""

import json
import re
import os
import sys
import time
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional, List
from enum import Enum
from collections import Counter

try:
    from openai import OpenAI
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("Install dependencies: pip install openai python-dotenv")
    print("Then set OPENAI_API_KEY in .env or environment")
    sys.exit(1)


# ─── Reference Parsing ──────────────────────────────────────────────────────

def parse_reference(article: dict) -> str:
    """
    Extract text content from a reference. Supports:
    - Pre-extracted text (in 'content' field)
    - PDF files (via 'path' field)
    - Web pages (via 'url' field)
    - Plain text files (via 'path' field)
    
    Returns the text content of the reference.
    """
    # If content already provided, use it
    if article.get("content") and len(article["content"].strip()) > 20:
        return article["content"]
    
    # Try loading from file path
    path = article.get("path", "")
    if path and os.path.exists(path):
        ext = Path(path).suffix.lower()
        
        if ext == ".pdf":
            return _parse_pdf(path)
        elif ext in (".txt", ".md", ".text"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        elif ext in (".html", ".htm"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return _extract_text_from_html(f.read())
        else:
            # Try reading as text
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    
    # Try fetching from URL
    url = article.get("url", "")
    if url and url.startswith("http"):
        return _fetch_url(url)
    
    # Fallback: use abstract or summary
    return article.get("abstract", "") or article.get("summary", "") or ""


def _parse_pdf(path: str) -> str:
    """Extract text from PDF using PyMuPDF (fitz)."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text.strip()
    except ImportError:
        print(f"  [WARN] PyMuPDF not installed. Cannot parse PDF: {path}")
        print(f"         Install with: pip install pymupdf")
        return ""
    except Exception as e:
        print(f"  [WARN] PDF parse error for {path}: {e}")
        return ""


def _fetch_url(url: str) -> str:
    """Fetch and extract text from a web page."""
    try:
        import requests
        from bs4 import BeautifulSoup
        
        headers = {"User-Agent": "Mozilla/5.0 HVE/1.0"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return _extract_text_from_html(resp.text)
    except ImportError:
        print(f"  [WARN] requests/beautifulsoup4 not installed. Cannot fetch: {url}")
        return ""
    except Exception as e:
        print(f"  [WARN] URL fetch error for {url}: {e}")
        return ""


def _extract_text_from_html(html: str) -> str:
    """Extract readable text from HTML."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["script", "style", "noscript"]):
            tag.decompose()
        root = soup.find("article") or soup.find("main") or soup.body or soup
        text = root.get_text("\n", strip=True)
        # Clean up excessive whitespace
        lines = [ln for ln in text.split("\n") if ln.strip()]
        return "\n".join(lines)[:15000]  # Cap at 15K chars
    except ImportError:
        # Fallback: basic tag removal
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()[:15000]


def prepare_references(articles: list) -> list:
    """Parse all references, populating 'content' field for each."""
    for article in articles:
        if not article.get("content") or len(article.get("content", "").strip()) < 20:
            article["content"] = parse_reference(article)
    return articles


# ─── Configuration ──────────────────────────────────────────────────────────

CONFIG = {
    "model": os.getenv("VERIFICATION_MODEL", "gpt-4o"),
    "temperature": 0.0,  # deterministic for verification
    "max_retries": 3,
    "rate_limit_delay": 1.0,  # seconds between API calls
    "strictness": "lenient",  # "lenient" (precision) | "strict" (recall)
}

# Strictness dial: which verdicts count as a detected hallucination.
#   lenient (default) -> flag clear non-support only  (favors precision)
#   strict            -> also flag partial support    (favors recall)
STRICTNESS_FLAGS = {
    "lenient": {"NOT_SUPPORTED", "CONTRADICTED", "FABRICATED"},
    "strict": {"PARTIALLY_SUPPORTED", "NOT_SUPPORTED", "CONTRADICTED", "FABRICATED"},
}


# ─── Enums & Data Classes ───────────────────────────────────────────────────

class CitationVerdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    FABRICATED = "FABRICATED"
    UNVERIFIABLE = "UNVERIFIABLE"


class EvalVerdict(str, Enum):
    ACCURATE = "ACCURATE"
    PARTIALLY_ACCURATE = "PARTIALLY_ACCURATE"
    INACCURATE = "INACCURATE"
    FABRICATED = "FABRICATED"
    UNVERIFIABLE = "UNVERIFIABLE"


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    MODERATE = "MODERATE"
    MINOR = "MINOR"


@dataclass
class ClaimVerification:
    question_id: str
    claim_id: str
    claim_text: str
    cited_refs: list
    verdict: str
    confidence: float
    evidence_quote: str
    evidence_reference: str  # which reference ID/number
    evidence_span_start: int  # character offset where evidence starts in the reference (-1 if not found)
    evidence_span_end: int  # character offset where evidence ends (-1 if not found)
    reasoning: str
    all_votes: list = None  # raw verdicts from each pass, if n_votes > 1 (None = single-pass mode)


@dataclass
class EvalVerification:
    question_id: str
    article_id: str
    eval_field: str
    source_value: str
    generated_value: str
    verdict: str
    severity: str
    reasoning: str
    confidence: float = 0.0


# ─── LLM Client ─────────────────────────────────────────────────────────────

client = None

def get_client():
    global client
    if client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY not set. Create a .env file or export it."
            )
        client = OpenAI(api_key=api_key)
    return client


def llm_call(system_prompt: str, user_prompt: str, response_format=None) -> str:
    """Make an LLM call with retries and rate limiting."""
    c = get_client()
    for attempt in range(CONFIG["max_retries"]):
        try:
            kwargs = {
                "model": CONFIG["model"],
                "temperature": CONFIG["temperature"],
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if response_format:
                kwargs["response_format"] = response_format
            response = c.chat.completions.create(**kwargs)
            time.sleep(CONFIG["rate_limit_delay"])
            return response.choices[0].message.content
        except Exception as e:
            if attempt < CONFIG["max_retries"] - 1:
                wait = (attempt + 1) * 5
                print(f"  [retry {attempt+1}] {e}, waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    return ""


def _clamp_confidence(value, default: float = 0.5) -> float:
    """Clamp an LLM-reported confidence to [0.0, 1.0] and round to 4 decimals.

    LLM confidence is a self-reported 0-1 float, but the model can return
    values outside the range (negative, > 1, NaN, Inf) or with arbitrary
    precision. This helper makes the value safe to serialize as JSON
    and easy to compare. Replaces the raw `result.get('confidence', 0.0)`
    pattern at every call site.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return round(default, 4)
    if v != v:  # NaN check
        return round(default, 4)
    if v == float("inf"):
        return 1.0
    if v == float("-inf"):
        return 0.0
    if v < 0.0:
        v = 0.0
    elif v > 1.0:
        v = 1.0
    return round(v, 4)


# ─── Task 1: Citation Accuracy Verification ─────────────────────────────────

CLAIM_DECOMPOSITION_PROMPT = """You are an expert at decomposing text into atomic, standalone factual claims.

Given a synopsis (answer) that contains inline citations like [1], [2], etc., break it down into individual claims.

For each claim:
1. Extract the atomic factual assertion
2. DECONTEXTUALIZE it: resolve all pronouns, coreferences, and implicit references so the claim is fully standalone and understandable without reading the rest of the synopsis. Replace "it", "this", "they", "the study", "these findings" etc. with the actual entities they refer to.
3. Identify which reference number(s) it cites

Rules:
- Each claim should be a single factual assertion that makes sense on its own
- Replace all pronouns and references with their actual referents (e.g., "It showed improvement" → "Creatine supplementation showed improvement in muscle strength")
- If a sentence makes multiple assertions, split them
- Include the citation numbers exactly as they appear
- Claims without citations should still be listed (with empty cited_refs)
- The claim_text must be fully self-contained — a reader should understand it without any other context

Return JSON:
{
    "claims": [
        {
            "claim_text": "fully standalone decontextualized claim",
            "cited_refs": [1, 3]
        }
    ]
}"""


CITATION_VERIFICATION_PROMPT = '''You are the cited source document. A claim has been made about your contents and attributed to you.

You are being asked directly: "Do you agree with this claim? Is this what you say?"

Given:
- A CLAIM that has been attributed to you
- YOUR CONTENT (the full text of what you actually say)
- The ORIGINAL QUESTION that prompted this claim

Based solely on your content, determine whether you actually support this claim.

Verdict options:
- SUPPORTED: Yes, I directly state this or it is logically inferable from what I say. This includes cases where my results/findings/experiments clearly demonstrate the claim even if I don't use those exact words.
- PARTIALLY_SUPPORTED: I discuss this topic and the gist is correct, but the claim adds specifics or wording I don't use. Also use if my work implicitly supports the claim (e.g., I show large models still hallucinate, which implicitly supports "scaling doesn't fix hallucination").
- NOT_SUPPORTED: I don't contain information relevant to this claim AT ALL — neither explicitly nor implicitly. This was not attributed to me correctly. The topic of the claim is unrelated to my content.
- CONTRADICTED: I explicitly say the opposite of this claim.
- UNVERIFIABLE: My content is unavailable or too sparse to assess.

IMPORTANT: Before choosing NOT_SUPPORTED, verify that the claim's topic is truly absent from your content. If you discuss the same general topic (even from a different angle), you MUST choose PARTIALLY_SUPPORTED — not NOT_SUPPORTED. Reserve NOT_SUPPORTED strictly for cases where your paper is about a completely different subject area (e.g., claim is about "creatine safety" but your paper is about "computer vision"). If there is ANY topical connection between the claim and your content, choose PARTIALLY_SUPPORTED.

HOWEVER: If the claim makes a COMPARATIVE statement (e.g., "X is superior/better/more effective than Y"), merely discussing X is NOT enough for PARTIALLY_SUPPORTED. The comparison itself must be present in your content. If you discuss X but never compare it to Y, the comparative claim is NOT_SUPPORTED.

Return JSON:
{
    "verdict": "SUPPORTED|PARTIALLY_SUPPORTED|NOT_SUPPORTED|CONTRADICTED|UNVERIFIABLE",
    "confidence": 0.0-1.0,
    "evidence_quote": "the EXACT verbatim quote from my content that supports or contradicts this claim (copy-paste, not paraphrase). Empty if not found.",
    "evidence_start_phrase": "the first 5 words of the evidence quote (for locating it in the document)",
    "reasoning": "brief explanation of my assessment"
}'''


def decompose_claims(synopsis: str) -> list:
    """Break synopsis into atomic claims with their citation references."""
    raw = llm_call(
        CLAIM_DECOMPOSITION_PROMPT,
        f"Synopsis:\n{synopsis}",
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(raw)
        return data.get("claims", [])
    except json.JSONDecodeError:
        print(f"  [WARN] Failed to parse claim decomposition")
        return []


def extract_cited_refs(text: str) -> list:
    """Deterministically extract [N] / [N, M] citation numbers from raw text via regex.
    No LLM call — used by --single-claim mode to avoid an unnecessary decomposition pass
    on text that is already a single atomic claim.
    """
    refs = []
    for match in re.finditer(r"\[(\d+(?:\s*,\s*\d+)*)\]", text):
        for num in match.group(1).split(","):
            n = int(num.strip())
            if n not in refs:
                refs.append(n)
    return refs


def _find_relevant_spans(claim_text: str, content: str, max_chars: int = 4000, window: int = 1000, top_chunks: int = 12, anchor_chars: int = 500) -> str:
    """
    Find the most relevant spans in a long document for a given claim.
    Uses keyword overlap to locate the best chunks, then returns them
    concatenated, with the first and last `anchor_chars` of the document
    always included as anchors so the LLM has context for intro/conclusion.

    C7 fix: finer granularity (window=1000, step=200) and more chunks
    (top 12 vs 8). The previous coarser window missed multi-clause claims
    whose key terms were spread across the document.

    This implements the HLD's "reference matching" — finding the most
    relevant spans in the reference for each claim.
    """
    # Extract key terms from the claim
    stop_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'been', 'be', 'have', 'has',
                  'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may', 'might',
                  'shall', 'can', 'need', 'dare', 'ought', 'used', 'to', 'of', 'in', 'for', 'on',
                  'with', 'at', 'by', 'from', 'as', 'into', 'through', 'during', 'before', 'after',
                  'that', 'this', 'these', 'those', 'it', 'its', 'and', 'or', 'but', 'not', 'no'}

    claim_words = set(w.lower().strip('.,;:()[]') for w in claim_text.split()
                     if len(w) > 3 and w.lower() not in stop_words)

    if not claim_words:
        return content[:max_chars]

    # For very short content (shorter than the window), no chunking is
    # needed; return as-is.
    if len(content) <= window:
        return content[:max_chars]

    # Always include the first and last `anchor_chars` of the document so
    # the LLM has intro/conclusion context even when key terms live in
    # the middle.
    anchors = []
    head = content[:anchor_chars]
    tail = content[-anchor_chars:] if len(content) > anchor_chars else ""
    if head:
        anchors.append((0, head))
    if tail and tail != head:
        anchors.append((len(content) - anchor_chars, tail))

    # Split content into overlapping windows and score each by keyword overlap
    chunks = []
    step = max(50, window // 5)  # 5 windows per window-width for finer granularity
    for i in range(0, len(content) - window, step):
        chunk = content[i:i + window]
        chunk_words = set(w.lower().strip('.,;:()[]') for w in chunk.split())
        score = len(claim_words & chunk_words)
        chunks.append((score, i, chunk))

    # Sort by relevance score, take top chunks
    chunks.sort(key=lambda x: -x[0])

    # Combine: top chunks (deduped against anchors by position) + anchors.
    # Then sort by position to maintain document order.
    selected_positions = set()
    result_parts = []  # list of (position, text)

    for score, pos, chunk in chunks[:top_chunks]:
        if score >= 2 and pos not in selected_positions:
            result_parts.append((pos, chunk))
            selected_positions.add(pos)

    for pos, anchor in anchors:
        if pos not in selected_positions:
            result_parts.append((pos, anchor))
            selected_positions.add(pos)

    result_parts.sort(key=lambda x: x[0])
    result = ""
    for pos, chunk in result_parts:
        if len(result) + len(chunk) + 10 > max_chars:
            break
        if result:
            result += "\n...\n"
        result += chunk
    return result


def _search_backup_reference(claim_text: str, question: str, question_id: str, claim_idx: int, search_fn) -> 'ClaimVerification':
    """
    Search approved databases for a reference that supports/refutes an uncited claim.
    
    This implements professor's directive: "find backup in user-approved databases
    for unverifiable statements" — provide references for uncited claims.
    
    Args:
        claim_text: The uncited claim to find backup for
        question: The original question for context
        question_id: ID of the question entry
        claim_idx: Index of this claim
        search_fn: A callable that takes a query string and returns a list of
                   {"title": str, "content": str, "url": str} dicts
    
    Returns:
        ClaimVerification with either:
        - SUPPORTED + the found reference as evidence
        - NOT_SUPPORTED if searched but no backup found
    """
    # Generate a search query from the claim
    search_query = claim_text[:200]  # Use the claim itself as the query
    
    try:
        search_results = search_fn(search_query)
    except Exception as e:
        return ClaimVerification(
            question_id=question_id,
            claim_id=f"{question_id}-C{claim_idx}",
            claim_text=claim_text,
            cited_refs=[],
            verdict="UNVERIFIABLE",
            confidence=0.5,
            evidence_quote="",
            evidence_reference="",
            evidence_span_start=-1,
            evidence_span_end=-1,
            reasoning=f"Backup database search failed: {e}",
        )
    
    if not search_results:
        return ClaimVerification(
            question_id=question_id,
            claim_id=f"{question_id}-C{claim_idx}",
            claim_text=claim_text,
            cited_refs=[],
            verdict="NO_BACKUP_FOUND",
            confidence=0.7,
            evidence_quote="",
            evidence_reference="",
            evidence_span_start=-1,
            evidence_span_end=-1,
            reasoning="Searched approved databases for backup evidence but found no supporting reference.",
        )
    
    # Verify the claim against search results (try top results until one supports)
    for search_result in search_results[:3]:
        content = search_result.get("content", "")
        title = search_result.get("title", "")
        url = search_result.get("url", "")
        
        if not content:
            continue
        
        # Use the same verification logic as cited claims
        relevant_chunk = _find_relevant_spans(claim_text, content) if len(content) > 4000 else content
        
        user_prompt = (
            f"ORIGINAL QUESTION: {question}\n\n"
            f"CLAIM TO VERIFY: {claim_text}\n\n"
            f"YOUR CONTENT (from database search result: {title}):\n{relevant_chunk}"
        )
        
        raw = llm_call(
            CITATION_VERIFICATION_PROMPT,
            user_prompt,
            response_format={"type": "json_object"},
        )
        
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {"verdict": "UNVERIFIABLE", "confidence": 0.5, "evidence_quote": "", "reasoning": "Parse error"}
        
        verdict = result.get("verdict", "UNVERIFIABLE")
        
        # If this result supports the claim, return it
        if verdict in ("SUPPORTED", "PARTIALLY_SUPPORTED"):
            if verdict == "SUPPORTED":
                final_verdict = "BACKUP_FOUND"
            else:
                final_verdict = "BACKUP_PARTIAL"
            
            return ClaimVerification(
                question_id=question_id,
                claim_id=f"{question_id}-C{claim_idx}",
                claim_text=claim_text,
                cited_refs=[],
                verdict=final_verdict,
                confidence=_clamp_confidence(result.get("confidence"), default=0.5),
                evidence_quote=result.get("evidence_quote", ""),
                evidence_reference=f"BACKUP: {title} ({url})",
                evidence_span_start=-1,
                evidence_span_end=-1,
                reasoning=f"Backup database search found a supporting source: '{title}'. {result.get('reasoning', '')}",
            )
    
    # None of the search results supported the claim
    return ClaimVerification(
        question_id=question_id,
        claim_id=f"{question_id}-C{claim_idx}",
        claim_text=claim_text,
        cited_refs=[],
        verdict="NO_BACKUP_FOUND",
        confidence=0.7,
        evidence_quote="",
        evidence_reference="",
        evidence_span_start=-1,
        evidence_span_end=-1,
        reasoning=f"Searched approved databases ({len(search_results)} results) for backup evidence, but none supported this claim.",
    )


def search_pubmed(query: str, max_results: int = 3) -> list:
    """
    Search PubMed for articles matching a query.
    Returns list of {"title": str, "content": str, "url": str}.

    This is the default search_fn for backup reference finding.
    """
    import requests

    # PubMed E-utilities search
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    # Step 1: Search for article IDs
    search_url = f"{base_url}/esearch.fcgi"
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
    }

    try:
        resp = requests.get(search_url, params=params, timeout=15)
        data = resp.json()
        ids = data.get("esearchresult", {}).get("idlist", [])
    except Exception:
        return []

    if not ids:
        return []

    # Step 2: Fetch abstracts for found articles
    fetch_url = f"{base_url}/efetch.fcgi"
    params = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "xml",
    }

    try:
        resp = requests.get(fetch_url, params=params, timeout=15)
        # M9 fix: use ElementTree to handle nested tags and CDATA
        # correctly. The previous regex approach broke on PubMed XML
        # where abstract sections can be split or have inner markup.
        import re
        import xml.etree.ElementTree as ET
        results = []
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            # Fall back to a coarse split if the document is malformed
            articles = resp.text.split("<PubmedArticle>")[1:]
            for article_xml in articles[:max_results]:
                title_m = re.search(r"<ArticleTitle>(.*?)</ArticleTitle>", article_xml, re.DOTALL)
                abstract_m = re.search(r"<AbstractText.*?>(.*?)</AbstractText>", article_xml, re.DOTALL)
                pmid_m = re.search(r"<PMID.*?>(.*?)</PMID>", article_xml)
                title = re.sub(r"<.*?>", "", title_m.group(1).strip()) if title_m else ""
                abstract = re.sub(r"<.*?>", "", abstract_m.group(1).strip()) if abstract_m else ""
                pmid = pmid_m.group(1).strip() if pmid_m else ""
                if title and abstract:
                    results.append({
                        "title": title,
                        "content": abstract,
                        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    })
            return results

        for article in root.findall(".//PubmedArticle"):
            title_el = article.find(".//ArticleTitle")
            abstract_els = article.findall(".//AbstractText")
            pmid_el = article.find(".//PMID")
            title = "".join(title_el.itertext()).strip() if title_el is not None else ""
            abstract = " ".join("".join(e.itertext()).strip() for e in abstract_els).strip()
            pmid = "".join(pmid_el.itertext()).strip() if pmid_el is not None else ""
            if title and abstract:
                results.append({
                    "title": title,
                    "content": abstract,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                })
            if len(results) >= max_results:
                break
        return results
    except Exception:
        return []


def verify_single_claim(
    claim: dict, articles: list, question: str, question_id: str, claim_idx: int,
    search_backup: bool = False, search_fn=None, n_votes: int = 1
) -> ClaimVerification:
    """Verify one claim against its cited sources.
    
    If search_backup=True and claim has no citations, searches approved databases
    for a reference that supports or refutes the claim.
    
    If n_votes > 1, verifies the claim n_votes times and returns the majority
    verdict. This controls for LLM non-determinism at the cost of n_votes API
    calls per claim. n_votes=3 was validated to reproduce our published benchmark
    numbers exactly (see arxiv_test/hn_scores_*_keyed.json).
    """
    cited_refs = claim.get("cited_refs", [])
    claim_text = claim.get("claim_text", "")

    if not cited_refs:
        # NEW: If search_backup enabled, try to find a reference for uncited claims
        if search_backup and search_fn:
            return _search_backup_reference(claim_text, question, question_id, claim_idx, search_fn)
        
        return ClaimVerification(
            question_id=question_id,
            claim_id=f"{question_id}-C{claim_idx}",
            claim_text=claim_text,
            cited_refs=[],
            verdict="UNVERIFIABLE",
            confidence=0.5,
            evidence_quote="",
            evidence_reference="",
            evidence_span_start=-1,
            evidence_span_end=-1,
            reasoning="No citation provided for this claim",
        )

    # Gather content from cited articles — find most relevant spans
    source_texts = []
    for ref_num in cited_refs:
        idx = ref_num - 1  # 0-indexed
        if 0 <= idx < len(articles):
            art = articles[idx]
            content = art.get("content") or art.get("abstract") or art.get("summary") or ""
            title = art.get("title", f"Article {ref_num}")
            
            # If content is short enough, use it all
            if len(content) <= 4000:
                source_texts.append(f"[{ref_num}] {title}\n{content}")
            else:
                # Find the most relevant spans for this claim
                relevant_chunk = _find_relevant_spans(claim_text, content)
                source_texts.append(f"[{ref_num}] {title}\n{relevant_chunk}")
        else:
            source_texts.append(f"[{ref_num}] (article not found in retrieved set)")

    source_block = "\n\n---\n\n".join(source_texts)

    user_prompt = (
        f"ORIGINAL QUESTION: {question}\n\n"
        f"CLAIM ATTRIBUTED TO YOU: {claim_text}\n\n"
        f"YOUR CONTENT:\n{source_block}"
    )

    # Run n_votes passes and collect parsed results
    passes = []
    for _ in range(max(1, n_votes)):
        raw = llm_call(
            CITATION_VERIFICATION_PROMPT,
            user_prompt,
            response_format={"type": "json_object"},
        )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {
                "verdict": "UNVERIFIABLE",
                "confidence": 0.0,
                "evidence_quote": "",
                "evidence_start_phrase": "",
                "reasoning": "Failed to parse verification response",
            }
        passes.append(parsed)

    all_votes = [p.get("verdict", "UNVERIFIABLE") for p in passes]

    if n_votes > 1:
        # Majority vote on verdict; break ties by keeping first-seen order
        vote_counts = Counter(all_votes)
        majority_verdict = vote_counts.most_common(1)[0][0]
        # Use the fields (confidence/evidence/reasoning) from the first pass
        # whose verdict matches the majority, so the report stays internally consistent
        result = next(p for p, v in zip(passes, all_votes) if v == majority_verdict)
    else:
        result = passes[0]

    # Compute span offset: find evidence_quote in the source content
    evidence_quote = result.get("evidence_quote", "")
    evidence_ref = str(cited_refs[0]) if cited_refs else ""
    span_start = -1
    span_end = -1

    if evidence_quote and cited_refs:
        # Try to locate the quote in the source article
        ref_idx = cited_refs[0] - 1
        if 0 <= ref_idx < len(articles):
            source_content = articles[ref_idx].get("content") or articles[ref_idx].get("abstract") or ""
            # Try exact match first
            pos = source_content.find(evidence_quote[:50])  # First 50 chars for matching
            if pos >= 0:
                span_start = pos
                span_end = pos + len(evidence_quote)
            else:
                # Try the start phrase from LLM response
                start_phrase = result.get("evidence_start_phrase", "")
                if start_phrase:
                    pos = source_content.find(start_phrase)
                    if pos >= 0:
                        span_start = pos
                        span_end = pos + len(evidence_quote)
                if pos < 0:
                    # Final fallback: normalized whitespace match.
                    # The LLM may have collapsed/different whitespace than
                    # the source (e.g. the LLM returned "Hello world" and
                    # the source has "Hello  world"). Normalize both and
                    # search again.
                    import re as _re
                    def _norm_ws(s):
                        return _re.sub(r"\s+", " ", s).strip()
                    src_norm = _norm_ws(source_content)
                    quote_norm = _norm_ws(evidence_quote[:50])
                    if quote_norm:
                        pos_norm = src_norm.find(quote_norm)
                        if pos_norm >= 0:
                            # Map back to the original source by finding the
                            # normalized position in a pre-normalized mapping
                            # of offsets. Approximate: search for the
                            # first 10 chars of the quote in the original.
                            head = quote_norm[:10]
                            pos = source_content.find(head)
                            if pos >= 0:
                                span_start = pos
                                span_end = pos + len(evidence_quote)

    return ClaimVerification(
        question_id=question_id,
        claim_id=f"{question_id}-C{claim_idx}",
        claim_text=claim_text,
        cited_refs=cited_refs,
        verdict=result.get("verdict", "UNVERIFIABLE"),
        confidence=_clamp_confidence(result.get("confidence"), default=0.0),
        evidence_quote=evidence_quote,
        evidence_reference=evidence_ref,
        evidence_span_start=span_start,
        evidence_span_end=span_end,
        reasoning=result.get("reasoning", ""),
        all_votes=all_votes if n_votes > 1 else None,
    )


# Aggregation order for per-ref verdicts. Higher index = stronger claim support.
# "Best ref wins": if any ref says SUPPORTED, the claim is at least partly supported.
_PER_REF_AGGREGATION_ORDER = [
    "UNVERIFIABLE",
    "CONTRADICTED",
    "NOT_SUPPORTED",
    "PARTIALLY_SUPPORTED",
    "SUPPORTED",
]


def verify_claim_per_ref(
    claim: dict, articles: list, question: str, question_id: str, claim_idx: int,
    search_backup: bool = False, search_fn=None, n_votes: int = 1
) -> ClaimVerification:
    """Verify one claim against each cited ref independently, then aggregate.

    The root cause of the 55-62% website drop was a single-LLM-call design
    that concatenated all cited sources into one source block, then asked
    the LLM to verify the whole sentence at once. For a 5-ref claim
    (e.g. "transformers [62,68] use self-attention; adapters [14,23] are
    parameter-efficient; noise [26,27] is a regularizer"), the LLM would
    see only one source's content covering ~1/5 of the sentence and
    return PARTIALLY_SUPPORTED.

    This function issues one LLM call per ref, then picks the strongest
    verdict (any SUPPORTED ref promotes the claim; otherwise any
    PARTIALLY; etc.). The returned ClaimVerification also has a
    `per_ref_verdicts` attribute attached (list of dicts with ref_num,
    verdict, confidence, evidence_quote, error) for callers that want
    the per-ref breakdown.

    Cost: N LLM calls per multi-ref claim vs 1 in the old path. Worth it;
    this is the headline number the website reports.
    """
    cited_refs = claim.get("cited_refs", [])
    claim_text = claim.get("claim_text", "")

    if not cited_refs:
        # No citations: fall through to the same uncited-claim path as
        # verify_single_claim (search_backup or UNVERIFIABLE).
        if search_backup and search_fn:
            return _search_backup_reference(claim_text, question, question_id, claim_idx, search_fn)
        return ClaimVerification(
            question_id=question_id,
            claim_id=f"{question_id}-C{claim_idx}",
            claim_text=claim_text,
            cited_refs=[],
            verdict="UNVERIFIABLE",
            confidence=0.5,
            evidence_quote="",
            evidence_reference="",
            evidence_span_start=-1,
            evidence_span_end=-1,
            reasoning="No citation provided for this claim",
        )

    per_ref_verdicts = []
    best_verdict = None
    best_rank = -1
    best_payload = None  # confidence/evidence/reasoning from the winning ref
    chosen_ref = None

    for ref_num in cited_refs:
        idx = ref_num - 1
        if not (0 <= idx < len(articles)):
            per_ref_verdicts.append({
                "ref_num": ref_num,
                "verdict": "UNVERIFIABLE",
                "confidence": 0.0,
                "evidence_quote": "",
                "reasoning": f"ref [{ref_num}] not in retrieved set",
                "error": "missing",
            })
            continue

        art = articles[idx]
        content = art.get("content") or art.get("abstract") or art.get("summary") or ""
        title = art.get("title", f"Article {ref_num}")
        if not content or len(content.strip()) < 20:
            per_ref_verdicts.append({
                "ref_num": ref_num,
                "verdict": "UNVERIFIABLE",
                "confidence": 0.0,
                "evidence_quote": "",
                "reasoning": f"ref [{ref_num}] has no resolvable content",
                "error": "no_content",
            })
            continue

        # Build a single-ref source block and call the LLM
        if len(content) <= 4000:
            relevant = content
        else:
            relevant = _find_relevant_spans(claim_text, content)
        source_block = f"[{ref_num}] {title}\n{relevant}"

        # H1: NLI pre-filter (optional, default off). If enabled and the
        # NLI model is confident (>= SHORT_CIRCUIT_*_THRESHOLD), skip the
        # LLM call for this ref entirely. Saves cost and latency on
        # high-confidence SUPPORTED/NOT_SUPPORTED verdicts.
        nli_short_circuited = False
        try:
            import hve_nli
            short = hve_nli.maybe_short_circuit(claim_text, source_block)
            if short is not None:
                verdict_label, conf, _eq = short
                ref_verdict = verdict_label
                ref_payload = {
                    "verdict": verdict_label,
                    "confidence": conf,
                    "evidence_quote": "",
                    "evidence_start_phrase": "",
                    "reasoning": f"short-circuited by NLI pre-filter (confidence {conf:.2f})",
                }
                nli_short_circuited = True
        except ImportError:
            pass  # hve_nli not on path; skip

        user_prompt = (
            f"ORIGINAL QUESTION: {question}\n\n"
            f"CLAIM ATTRIBUTED TO YOU: {claim_text}\n\n"
            f"YOUR CONTENT:\n{source_block}"
        ) if not nli_short_circuited else ""

        if nli_short_circuited:
            # Skip LLM call entirely; ref_verdict and ref_payload already set
            passes = [ref_payload]
        else:
            # n_votes: majority over the votes for this single ref
            passes = []
            for _ in range(max(1, n_votes)):
                raw = llm_call(
                    CITATION_VERIFICATION_PROMPT,
                    user_prompt,
                    response_format={"type": "json_object"},
                )
                try:
                    passes.append(json.loads(raw))
                except json.JSONDecodeError:
                    passes.append({
                        "verdict": "UNVERIFIABLE",
                        "confidence": 0.0,
                        "evidence_quote": "",
                        "evidence_start_phrase": "",
                        "reasoning": "Failed to parse verification response",
                    })

            if n_votes > 1:
                vote_counts = Counter(p.get("verdict", "UNVERIFIABLE") for p in passes)
                ref_verdict = vote_counts.most_common(1)[0][0]
                ref_payload = next(p for p, v in zip(passes, [pp.get("verdict", "UNVERIFIABLE") for pp in passes]) if v == ref_verdict)
            else:
                ref_verdict = passes[0].get("verdict", "UNVERIFIABLE")
                ref_payload = passes[0]

        ref_confidence = float(ref_payload.get("confidence", 0.0))
        per_ref_verdicts.append({
            "ref_num": ref_num,
            "verdict": ref_verdict,
            "confidence": ref_confidence,
            "evidence_quote": ref_payload.get("evidence_quote", ""),
            "reasoning": ref_payload.get("reasoning", ""),
        })

        # Aggregate: take the strongest verdict
        rank = _PER_REF_AGGREGATION_ORDER.index(ref_verdict) if ref_verdict in _PER_REF_AGGREGATION_ORDER else -1
        if rank > best_rank:
            best_rank = rank
            best_verdict = ref_verdict
            best_payload = ref_payload
            chosen_ref = ref_num

    if best_verdict is None:
        # No ref had any content at all. Dispatch to search-backup if enabled.
        if search_backup:
            fn = search_fn if search_fn is not None else search_pubmed
            result = _search_backup_reference(claim_text, question, question_id, claim_idx, fn)
            setattr(result, "per_ref_verdicts", per_ref_verdicts)
            return result
        return ClaimVerification(
            question_id=question_id,
            claim_id=f"{question_id}-C{claim_idx}",
            claim_text=claim_text,
            cited_refs=cited_refs,
            verdict="UNVERIFIABLE",
            confidence=0.0,
            evidence_quote="",
            evidence_reference="",
            evidence_span_start=-1,
            evidence_span_end=-1,
            reasoning="No cited source had resolvable content",
        )

    # If every accessible ref returned UNVERIFIABLE and search-backup is enabled,
    # try to find backup evidence rather than reporting the claim as unverifiable.
    # This is Dennis's 6 Aug directive ("HallucinationNerd would find hallucinations
    # when references are given and provide references for unverifiable statements").
    # Only dispatch when at least one ref was accessible (otherwise we already
    # dispatched above) AND every accessible ref came back UNVERIFIABLE.
    accessible_refs = [e for e in per_ref_verdicts if e.get("error") is None]
    all_unverifiable = (
        best_verdict == "UNVERIFIABLE"
        and len(accessible_refs) > 0
        and all(e["verdict"] == "UNVERIFIABLE" for e in accessible_refs)
    )
    if all_unverifiable and search_backup:
        fn = search_fn if search_fn is not None else search_pubmed
        result = _search_backup_reference(claim_text, question, question_id, claim_idx, fn)
        setattr(result, "per_ref_verdicts", per_ref_verdicts)
        return result

    evidence_quote = best_payload.get("evidence_quote", "")
    evidence_ref = str(chosen_ref) if chosen_ref is not None else ""

    # Compute span offset in the chosen source (best-effort). C10 fix:
    # fallback chain is now (1) exact prefix match, (2) start-phrase match,
    # (3) normalized-whitespace match.
    span_start = -1
    span_end = -1
    if evidence_quote and chosen_ref is not None and 0 <= (chosen_ref - 1) < len(articles):
        ref_idx = chosen_ref - 1
        source_content = articles[ref_idx].get("content") or articles[ref_idx].get("abstract") or ""
        if source_content:
            pos = source_content.find(evidence_quote[:50])
            if pos >= 0:
                span_start = pos
                span_end = pos + len(evidence_quote)
            else:
                start_phrase = best_payload.get("evidence_start_phrase", "")
                if start_phrase:
                    pos = source_content.find(start_phrase)
                    if pos >= 0:
                        span_start = pos
                        span_end = pos + len(evidence_quote)
                if pos < 0:
                    # Normalized-whitespace fallback
                    import re as _re
                    def _norm_ws(s):
                        return _re.sub(r"\s+", " ", s).strip()
                    src_norm = _norm_ws(source_content)
                    quote_norm = _norm_ws(evidence_quote[:50])
                    if quote_norm:
                        pos_norm = src_norm.find(quote_norm)
                        if pos_norm >= 0:
                            head = quote_norm[:10]
                            pos = source_content.find(head)
                            if pos >= 0:
                                span_start = pos
                                span_end = pos + len(evidence_quote)

    result = ClaimVerification(
        question_id=question_id,
        claim_id=f"{question_id}-C{claim_idx}",
        claim_text=claim_text,
        cited_refs=cited_refs,
        verdict=best_verdict,
        confidence=_clamp_confidence(best_payload.get("confidence"), default=0.0),
        evidence_quote=evidence_quote,
        evidence_reference=evidence_ref,
        evidence_span_start=span_start,
        evidence_span_end=span_end,
        reasoning=(
            f"Per-ref verification: {len(per_ref_verdicts)} ref(s) evaluated; "
            f"strongest ref [{chosen_ref}] returned {best_verdict}. "
            + best_payload.get("reasoning", "")
        ),
    )
    # Attach the per-ref breakdown as a non-standard attribute for callers
    # that want the full picture. Dataclass allows this.
    setattr(result, "per_ref_verdicts", per_ref_verdicts)
    return result


def verify_citations_for_question(entry: dict, search_backup: bool = False, n_votes: int = 1, single_claim: bool = False, per_ref: bool = False) -> list:
    """Run full citation verification for one Q&A entry.

    If single_claim=True, the entire synopsis is treated as one atomic claim
    (citation numbers extracted deterministically via regex) instead of being
    passed through decompose_claims(). Use this when the input is already
    pre-atomized (one claim, one citation per entry) — e.g. benchmark datasets
    built from Related Work clauses — to avoid an unnecessary, non-deterministic
    LLM-based splitting pass that changes the unit of evaluation.

    If per_ref=True, each cited ref is verified independently and the
    verdicts are aggregated (any SUPPORTED ref promotes the claim).
    This is the correct behavior for multi-ref claims. The benchmark
    path uses per_ref=False for backward compatibility (the pre-paired
    benchmark only has 1 ref per claim, so the per-ref vs. single-call
    distinction is moot); the website path uses per_ref=True.
    """
    question_id = entry.get("question_id", "UNKNOWN")
    question = entry.get("question", "")
    synopsis = entry.get("synopsis", entry.get("end_output", ""))

    # Use retrieved_articles (has full content) over citations_obj (matched subset)
    articles = entry.get("retrieved_articles", entry.get("citations_obj", []))

    # Parse references (PDF, web, etc.) if content not already provided
    articles = prepare_references(articles)

    if not synopsis:
        print(f"  [{question_id}] No synopsis found, skipping")
        return []

    if single_claim:
        claims = [{
            "claim_text": synopsis,
            "cited_refs": extract_cited_refs(synopsis),
        }]
        print(f"  [{question_id}] Single-claim mode: 1 claim, cited_refs={claims[0]['cited_refs']}")
    else:
        print(f"  [{question_id}] Decomposing claims...")
        claims = decompose_claims(synopsis)
        print(f"  [{question_id}] Found {len(claims)} claims, verifying...")

    results = []
    for i, claim in enumerate(claims, 1):
        if per_ref:
            result = verify_claim_per_ref(
                claim, articles, question, question_id, i,
                search_backup=search_backup,
                search_fn=search_pubmed if search_backup else None,
                n_votes=n_votes,
            )
        else:
            result = verify_single_claim(
                claim, articles, question, question_id, i,
                search_backup=search_backup,
                search_fn=search_pubmed if search_backup else None,
                n_votes=n_votes,
            )
        results.append(result)

    return results


# ─── Task 2: Article Evaluation Verification ────────────────────────────────

EVAL_VERIFICATION_PROMPT = """You are an expert fact-checker verifying whether an AI-generated article evaluation/summary is faithful to the source article.

Given:
- The SOURCE ARTICLE content (the actual text of the article/post)
- The GENERATED EVALUATION (what the AI produced about this article)
- The specific FIELD being checked

Determine whether the generated evaluation is accurate for this field.

Verdict options:
- ACCURATE: The generated value correctly represents what's in the source.
- PARTIALLY_ACCURATE: Mostly correct but with minor inaccuracies or unsupported additions.
- INACCURATE: Significantly misrepresents the source content.
- FABRICATED: Contains information that is completely invented, not from the source.
- UNVERIFIABLE: Source content insufficient to verify.

Severity options:
- CRITICAL: Changes the meaning or could mislead (wrong stats, wrong conclusion, invented facts)
- MODERATE: Adds unsupported details that don't fundamentally change meaning
- MINOR: Slight rephrasing issues, trivial inaccuracies

Return JSON:
{
    "verdict": "ACCURATE|PARTIALLY_ACCURATE|INACCURATE|FABRICATED|UNVERIFIABLE",
    "severity": "CRITICAL|MODERATE|MINOR",
    "reasoning": "brief explanation"
}"""


# Fields to check for CloudNerd (ABSTRACT_EXTRACTION_PROMPT output)
CLOUDNERD_EVAL_FIELDS = [
    "problem",
    "accepted_solution",
    "commands_config_code",
    "error_messages_versions",
    "relevant_services_tools",
    "risks_caveats",
]

# Fields to check for DietNerd (STUDY_SUMMARY_PROMPT / REVIEW_SUMMARY_PROMPT output)
DIETNERD_EVAL_FIELDS = [
    "purpose_design",
    "main_conclusions",
    "risks",
    "benefits",
    "statistical_analysis",
    "significance_level",
    "confidence_interval",
    "effect_size",
    "conflict_of_interest",
    "size_of_study",
]


def verify_article_evaluation(
    question_id: str, article: dict, source_content: str
) -> list:
    """Verify all evaluation fields for one article."""
    results = []
    article_id = article.get("id") or article.get("doi") or article.get("url") or article.get("title", "")[:50]

    # Determine which fields to check based on available data
    summary = article.get("summary", "")
    abstract = article.get("abstract", "")

    # The summary is the main generated evaluation to verify
    if not summary and not abstract:
        return []

    # For CloudNerd, check the structured extraction fields if present
    eval_content = article.get("structured_extraction", {})
    if not eval_content and summary:
        # Parse the summary into checkable assertions
        eval_content = {"full_summary": summary}

    for field, value in eval_content.items():
        if not value or value == "Not Detected":
            continue

        user_prompt = (
            f"SOURCE ARTICLE:\n{source_content[:4000]}\n\n"
            f"FIELD: {field}\n"
            f"GENERATED VALUE: {value}"
        )

        raw = llm_call(
            EVAL_VERIFICATION_PROMPT,
            user_prompt,
            response_format={"type": "json_object"},
        )

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {
                "verdict": "UNVERIFIABLE",
                "severity": "MINOR",
                "reasoning": "Failed to parse verification response",
            }

        results.append(EvalVerification(
            question_id=question_id,
            article_id=article_id,
            eval_field=field,
            source_value=source_content[:500],
            generated_value=str(value)[:500],
            verdict=result.get("verdict", "UNVERIFIABLE"),
            severity=result.get("severity", "MINOR"),
            confidence=_clamp_confidence(result.get("confidence"), default=0.0),
            reasoning=result.get("reasoning", ""),
        ))

    # Always verify the summary against the source
    if summary and "full_summary" not in eval_content:
        user_prompt = (
            f"SOURCE ARTICLE:\n{source_content[:4000]}\n\n"
            f"FIELD: overall_summary\n"
            f"GENERATED VALUE: {summary}"
        )

        raw = llm_call(
            EVAL_VERIFICATION_PROMPT,
            user_prompt,
            response_format={"type": "json_object"},
        )

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {"verdict": "UNVERIFIABLE", "severity": "MINOR", "reasoning": "parse error"}

        results.append(EvalVerification(
            question_id=question_id,
            article_id=article_id,
            eval_field="overall_summary",
            source_value=source_content[:500],
            generated_value=summary[:500],
            verdict=result.get("verdict", "UNVERIFIABLE"),
            severity=result.get("severity", "MINOR"),
            confidence=_clamp_confidence(result.get("confidence"), default=0.0),
            reasoning=result.get("reasoning", ""),
        ))

    return results


# ─── Aggregate Reporting ────────────────────────────────────────────────────

def compute_citation_metrics(results: list) -> dict:
    """Compute aggregate citation accuracy metrics."""
    if not results:
        return {}

    total = len(results)
    by_verdict = {}
    for r in results:
        v = r.verdict if isinstance(r, ClaimVerification) else r["verdict"]
        by_verdict[v] = by_verdict.get(v, 0) + 1

    supported = by_verdict.get("SUPPORTED", 0)
    partial = by_verdict.get("PARTIALLY_SUPPORTED", 0)
    not_supported = by_verdict.get("NOT_SUPPORTED", 0)
    fabricated = by_verdict.get("FABRICATED", 0)
    unverifiable = by_verdict.get("UNVERIFIABLE", 0)
    contradicted = by_verdict.get("CONTRADICTED", 0)

    verifiable = total - unverifiable
    citation_precision = supported / verifiable if verifiable > 0 else 0

    # Strictness controls which verdicts count as a detected hallucination.
    #   lenient (default): flag NOT_SUPPORTED + CONTRADICTED (favor precision)
    #   strict:            also flag PARTIALLY_SUPPORTED (favor recall)
    flagged_verdicts = STRICTNESS_FLAGS.get(CONFIG.get("strictness", "lenient"),
                                            {"NOT_SUPPORTED", "CONTRADICTED", "FABRICATED"})
    flagged = sum(by_verdict.get(v, 0) for v in flagged_verdicts)
    hallucination_rate = flagged / verifiable if verifiable > 0 else 0
    if verifiable == 0:
        overall_verdict = "unverifiable"
    elif (not_supported + fabricated + contradicted) == 0:
        overall_verdict = "reliable"
    elif (not_supported + fabricated + contradicted) / verifiable > 0.3:
        overall_verdict = "unreliable"
    else:
        overall_verdict = "mixed"

    return {
        "total_claims": total,
        "supported": supported,
        "partially_supported": partial,
        "not_supported": not_supported,
        "contradicted": contradicted,
        "fabricated": by_verdict.get("FABRICATED", 0),
        "unverifiable": unverifiable,
        "citation_precision": round(citation_precision, 4),
        "hallucination_rate": round(hallucination_rate, 4),
        "partial_rate": round(partial / verifiable if verifiable else 0, 4),
        "overall_verdict": overall_verdict,
    }


def compute_eval_metrics(results: list) -> dict:
    """Compute aggregate article evaluation metrics."""
    if not results:
        return {}

    total = len(results)
    by_verdict = {}
    by_severity = {}
    for r in results:
        v = r.verdict if isinstance(r, EvalVerification) else r["verdict"]
        s = r.severity if isinstance(r, EvalVerification) else r["severity"]
        by_verdict[v] = by_verdict.get(v, 0) + 1
        by_severity[s] = by_severity.get(s, 0) + 1

    accurate = by_verdict.get("ACCURATE", 0)
    critical = by_severity.get("CRITICAL", 0)

    return {
        "total_evaluations": total,
        "accurate": accurate,
        "accuracy_rate": round(accurate / total if total else 0, 4),
        "critical_errors": critical,
        "critical_error_rate": round(critical / total if total else 0, 4),
        "by_verdict": by_verdict,
        "by_severity": by_severity,
    }


# ─── Main Pipeline ──────────────────────────────────────────────────────────

def run_citation_verification(data: list, output_dir: Path, search_backup: bool = False, n_votes: int = 1, single_claim: bool = False, per_ref: bool = False) -> dict:
    """Run citation verification across all questions."""
    print(f"\n{'='*60}")
    print("TASK 1: CITATION ACCURACY VERIFICATION")
    if search_backup:
        print("  [BACKUP MODE] Searching PubMed for uncited claims")
    if n_votes > 1:
        print(f"  [STABLE MODE] {n_votes}-vote majority per claim (controls for LLM non-determinism)")
    if single_claim:
        print("  [SINGLE-CLAIM MODE] Treating each entry's synopsis as one atomic claim (no decomposition)")
    if per_ref:
        print("  [PER-REF MODE] Verifying each cited ref independently (correct for multi-ref claims)")
    print(f"{'='*60}")
    print(f"Processing {len(data)} questions...")

    all_results = []
    per_question = {}

    for i, entry in enumerate(data, 1):
        qid = entry.get("question_id", f"Q{i}")
        print(f"\n[{i}/{len(data)}] Question: {qid}")
        results = verify_citations_for_question(entry, search_backup=search_backup, n_votes=n_votes, single_claim=single_claim, per_ref=per_ref)
        all_results.extend(results)
        per_question[qid] = compute_citation_metrics(results)

    # Write results
    output_file = output_dir / "citation_verification.jsonl"
    with open(output_file, "w") as f:
        for r in all_results:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    metrics = compute_citation_metrics(all_results)
    metrics["per_question"] = per_question
    print(f"\n✓ Citation verification complete: {output_file}")
    print(f"  Total claims: {metrics.get('total_claims', 0)}")
    print(f"  Precision: {metrics.get('citation_precision', 0):.1%}")
    print(f"  Hallucination rate: {metrics.get('hallucination_rate', 0):.1%}")

    return metrics


def run_evaluation_verification(data: list, output_dir: Path) -> dict:
    """Run article evaluation verification across all questions."""
    print(f"\n{'='*60}")
    print("TASK 2: ARTICLE EVALUATION ACCURACY VERIFICATION")
    print(f"{'='*60}")
    print(f"Processing {len(data)} questions...")

    # M2: skip eval task entirely if the dataset has nothing evaluatable
    has_eval = any(
        (a.get("summary") or a.get("structured_extraction"))
        for entry in data
        for a in entry.get("retrieved_articles", entry.get("citations_obj", []))
    )
    if not has_eval:
        print("  [SKIP] No 'summary' or 'structured_extraction' fields in dataset; nothing to evaluate.")
        return {"total_evaluations": 0, "accuracy_rate": 0.0, "by_verdict": {}, "by_severity": {}}

    all_results = []

    for i, entry in enumerate(data, 1):
        qid = entry.get("question_id", f"Q{i}")
        articles = entry.get("retrieved_articles", entry.get("citations_obj", []))

        print(f"\n[{i}/{len(data)}] Question: {qid} ({len(articles)} articles)")

        for article in articles:
            source = article.get("content") or article.get("abstract") or ""
            if not source:
                continue
            results = verify_article_evaluation(qid, article, source)
            all_results.extend(results)

    # Write results
    output_file = output_dir / "evaluation_verification.jsonl"
    with open(output_file, "w") as f:
        for r in all_results:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    metrics = compute_eval_metrics(all_results)
    print(f"\n✓ Evaluation verification complete: {output_file}")
    print(f"  Total evaluations: {metrics.get('total_evaluations', 0)}")
    print(f"  Accuracy rate: {metrics.get('accuracy_rate', 0):.1%}")
    print(f"  Critical errors: {metrics.get('critical_errors', 0)}")

    return metrics


def write_summary_report(citation_metrics: dict, eval_metrics: dict, output_dir: Path):
    """Write aggregate summary report."""
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_used": CONFIG["model"],
        "strictness": CONFIG.get("strictness", "lenient"),
        "rate_limit_delay": CONFIG.get("rate_limit_delay", 1.0),
        "citation_verification": citation_metrics,
        "evaluation_verification": eval_metrics,
    }
    output_file = output_dir / "summary_report.json"
    with open(output_file, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n✓ Summary report: {output_file}")

    # Also generate Markdown report
    write_markdown_report(citation_metrics, eval_metrics, output_dir)


def write_markdown_report(citation_metrics: dict, eval_metrics: dict, output_dir: Path):
    """Generate a human-readable Markdown verification report."""
    lines = []
    lines.append("# HVE Verification Report")
    lines.append("")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    lines.append(f"Model: {CONFIG['model']}")
    lines.append("")

    if citation_metrics:
        cm = citation_metrics
        lines.append("## Citation Verification")
        lines.append("")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Total claims | {cm.get('total_claims', 0)} |")
        lines.append(f"| Supported | {cm.get('supported', 0)} |")
        lines.append(f"| Partially supported | {cm.get('partially_supported', 0)} |")
        lines.append(f"| Not supported | {cm.get('not_supported', 0)} |")
        lines.append(f"| Contradicted | {cm.get('contradicted', 0)} |")
        lines.append(f"| Unverifiable | {cm.get('unverifiable', 0)} |")
        lines.append(f"| **Citation precision** | **{cm.get('citation_precision', 0):.1%}** |")
        lines.append(f"| **Hallucination rate** | **{cm.get('hallucination_rate', 0):.1%}** |")
        lines.append(f"| **Overall verdict** | **{cm.get('overall_verdict', 'N/A')}** |")
        lines.append("")

    if eval_metrics:
        em = eval_metrics
        lines.append("## Article Evaluation")
        lines.append("")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Total evaluations | {em.get('total_evaluations', 0)} |")
        lines.append(f"| Accurate | {em.get('accurate', 0)} |")
        lines.append(f"| Accuracy rate | {em.get('accuracy_rate', 0):.1%} |")
        lines.append(f"| Critical errors | {em.get('critical_errors', 0)} |")
        lines.append("")

    # Per-claim details from flagged cases
    cit_file = output_dir / "citation_verification.jsonl"
    if cit_file.exists():
        lines.append("## Flagged Claims")
        lines.append("")
        with open(cit_file) as f:
            flagged = [json.loads(l) for l in f if json.loads(l).get("verdict") in ("NOT_SUPPORTED", "CONTRADICTED", "FABRICATED")]
        if flagged:
            for r in flagged:
                lines.append(f"### {r['claim_id']} — {r['verdict']}")
                lines.append(f"- **Claim:** {r['claim_text']}")
                lines.append(f"- **Cited:** [{', '.join(str(x) for x in r.get('cited_refs', []))}]")
                lines.append(f"- **Reasoning:** {r.get('reasoning', '')}")
                lines.append("")
        else:
            lines.append("No hallucinations detected. ✓")
            lines.append("")

    output_file = output_dir / "verification_report.md"
    with open(output_file, "w") as f:
        f.write("\n".join(lines))
    print(f"✓ Markdown report: {output_file}")


def flag_severe_cases(output_dir: Path):
    """Extract high-severity cases for manual review."""
    flagged = []

    # Check citation results
    cit_file = output_dir / "citation_verification.jsonl"
    if cit_file.exists():
        with open(cit_file) as f:
            for line in f:
                r = json.loads(line)
                if r["verdict"] in ("FABRICATED", "NOT_SUPPORTED") and r["confidence"] >= 0.7:
                    flagged.append({"type": "citation", **r})

    # Check evaluation results
    eval_file = output_dir / "evaluation_verification.jsonl"
    if eval_file.exists():
        with open(eval_file) as f:
            for line in f:
                r = json.loads(line)
                if r["severity"] == "CRITICAL" and r["verdict"] in ("FABRICATED", "INACCURATE"):
                    flagged.append({"type": "evaluation", **r})

    output_file = output_dir / "flagged_cases.jsonl"
    with open(output_file, "w") as f:
        for case in flagged:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")

    print(f"\n✓ Flagged {len(flagged)} high-severity cases: {output_file}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Hallucination verification pipeline for CustomNerd/CloudNerd"
    )
    parser.add_argument(
        "--input", "-i", required=True,
        help="Path to input dataset (JSON array of Q&A entries)"
    )
    parser.add_argument(
        "--output-dir", "-o", default="results",
        help="Output directory for results (default: results/)"
    )
    parser.add_argument(
        "--task", "-t", choices=["citation", "evaluation", "both"], default="both",
        help="Which verification task to run (default: both)"
    )
    parser.add_argument(
        "--limit", "-n", type=int, default=None,
        help="Limit to first N questions (for testing)"
    )
    parser.add_argument(
        "--model", "-m", default=None,
        help=f"Override verification model (default: {CONFIG['model']})"
    )
    parser.add_argument(
        "--search-backup", action="store_true",
        help="Search PubMed for backup references for uncited claims (instead of marking UNVERIFIABLE)"
    )
    parser.add_argument(
        "--stable", action="store_true",
        help="Verify each claim 3 times and take the majority verdict, to control for LLM "
             "non-determinism (3x API cost/latency per claim). Equivalent to --votes 3."
    )
    parser.add_argument(
        "--votes", type=int, default=None,
        help="Number of verification passes per claim, majority vote wins (default: 1, or 3 if --stable is set)"
    )
    parser.add_argument(
        "--single-claim", action="store_true",
        help="Treat each entry's synopsis as one atomic claim instead of decomposing it into "
             "sub-claims via an LLM pass. Use this for pre-atomized benchmark data (one claim, "
             "one citation per entry) — this is the mode used to produce the paper's reported numbers."
    )
    parser.add_argument(
        "--per-ref", action="store_true",
        help="Verify each cited ref independently and aggregate verdicts (any SUPPORTED ref "
             "promotes the claim). This is the correct mode for multi-ref claims. The benchmark "
             "path uses single-call (default) for backward compatibility with the published "
             "numbers; the website uses per-ref."
    )
    parser.add_argument(
        "--strictness", choices=["lenient", "strict"], default="lenient",
        help="Strictness dial for flagging hallucinations. 'lenient' (default) flags only clear "
             "non-support (NOT_SUPPORTED/CONTRADICTED) — favors precision. 'strict' also flags "
             "PARTIALLY_SUPPORTED claims — favors recall."
    )

    args = parser.parse_args()
    CONFIG["strictness"] = args.strictness

    if args.model:
        CONFIG["model"] = args.model

    n_votes = args.votes if args.votes is not None else (3 if args.stable else 1)

    # Load input data
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        sys.exit(1)

    with open(input_path) as f:
        data = json.load(f)

    if isinstance(data, dict):
        # Handle case where data is wrapped in a key
        data = data.get("questions", data.get("results", [data]))

    if args.limit:
        data = data[:args.limit]

    print(f"Loaded {len(data)} entries from {input_path}")
    print(f"Using model: {CONFIG['model']}")

    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Run verification
    citation_metrics = {}
    eval_metrics = {}

    if args.task in ("citation", "both"):
        citation_metrics = run_citation_verification(data, output_dir, search_backup=args.search_backup, n_votes=n_votes, single_claim=args.single_claim, per_ref=args.per_ref)

    if args.task in ("evaluation", "both"):
        eval_metrics = run_evaluation_verification(data, output_dir)

    # Generate reports
    write_summary_report(citation_metrics, eval_metrics, output_dir)
    flag_severe_cases(output_dir)

    print(f"\n{'='*60}")
    print("VERIFICATION COMPLETE")
    print(f"{'='*60}")
    print(f"Results directory: {output_dir}/")


if __name__ == "__main__":
    main()
