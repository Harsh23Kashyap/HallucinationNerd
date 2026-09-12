"""
HallucinationNerd Web — Citation Hallucination Verification Service
FastAPI + Uvicorn + Jinja2
"""

import os
import sys
import json
import asyncio
import tempfile
import hashlib
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

# The canonical verification engine lives in the repository ROOT
# (verify_hallucinations.py), while web/ holds a thin re-export shim. When the
# app is started from web/ (Render Root Directory = web), the root must be on
# sys.path FIRST so `import verify_hallucinations` resolves to the real engine
# rather than the shim shadowing it. Insert it ahead of the web/ directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The verification engine files (verify_hallucinations.py, arxiv_extractor.py)
# are co-located in this directory for deployment.

# M19: per-IP rate limiting on /verify. Default: 5 requests per 60s.
# Override with HVE_RATE_LIMIT_REQUESTS and HVE_RATE_LIMIT_WINDOW_SECONDS.
# Uses a simple in-memory token bucket — single-process only. For
# multi-worker deployments, swap in a Redis-backed limiter.
import time as _time
_rate_limit_requests = int(os.getenv("HVE_RATE_LIMIT_REQUESTS", "5"))
_rate_limit_window = int(os.getenv("HVE_RATE_LIMIT_WINDOW_SECONDS", "60"))
_rate_buckets: dict = {}  # ip -> [timestamps]


def _check_rate_limit(ip: str) -> bool:
    """Returns True if the request is allowed, False if rate-limited."""
    now = _time.time()
    bucket = _rate_buckets.get(ip, [])
    # Drop timestamps outside the window
    bucket = [t for t in bucket if now - t < _rate_limit_window]
    if len(bucket) >= _rate_limit_requests:
        _rate_buckets[ip] = bucket
        return False
    bucket.append(now)
    _rate_buckets[ip] = bucket
    return True


app = FastAPI(title="HallucinationNerd", description="Citation Hallucination Verification")

# CORS: the static frontend (S3/CloudFront at hallucinationnerd.org) calls this API
# cross-origin. Allowed origins are configurable via HVE_ALLOWED_ORIGINS (comma-
# separated); defaults cover the production domain and local development.
from fastapi.middleware.cors import CORSMiddleware
_default_origins = "https://hallucinationnerd.org,https://www.hallucinationnerd.org,http://localhost:8000,http://127.0.0.1:8000"
_allowed_origins = [o.strip() for o in os.getenv("HVE_ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Serve the main page."""
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/verify")
async def verify_document(
    request: Request,
    file: UploadFile = File(...),
    source_type: str = Form(default="auto"),
    databases: str = Form(default=""),
    custom_database: str = Form(default=""),
):
    """
    Accept a document upload, extract claims + citations, verify each one.
    For claims with no inline citation, optionally run a backup search over the
    user-selected databases (comma-separated keys in `databases`, plus an optional
    `custom_database` search-URL template containing '{query}').
    Returns JSON with per-claim results.
    """
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="Server misconfigured: no API key")

    # M19: per-IP rate limit. Reject with 429 if the caller has burned
    # through their budget in the current window.
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {_rate_limit_requests} requests per {_rate_limit_window}s. Try again later.",
        )

    # L24: enforce a max upload size to prevent abuse. Default 25 MB.
    max_bytes = int(os.getenv("HVE_MAX_UPLOAD_BYTES", "26214400"))
    content_bytes = await file.read()
    if len(content_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(content_bytes)} bytes; max {max_bytes})",
        )
    filename = file.filename or "uploaded_file"
    suffix = Path(filename).suffix.lower()

    # Save to temp file for processing
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content_bytes)
        tmp_path = tmp.name

    db_list = [d.strip() for d in databases.split(",") if d.strip()]
    try:
        results = await asyncio.to_thread(
            _run_verification, tmp_path, filename, suffix, source_type, db_list, custom_database.strip()
        )
        return JSONResponse(content=results)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        os.unlink(tmp_path)


def _extract_clause_for_ref(claim_text: str, ref_key: str) -> Optional[str]:
    """
    For a claim citing many refs like "methods use attention [62,68], adapters [14,23], noise [26,27]",
    extract just the clause relevant to the ref we're verifying against.
    E.g., if ref_key="14", return "specialized adapters" context.
    """
    import re
    # Find where this ref number appears in the claim
    # Look for [X, ref_key, Y] or [ref_key] patterns
    patterns = [
        rf'\[([^\]]*\b{ref_key}\b[^\]]*)\]',  # [14, 23] containing our ref
    ]
    for pattern in patterns:
        match = re.search(pattern, claim_text)
        if match:
            # Get text before this citation bracket (the clause it's attached to)
            pos = match.start()
            # Walk backwards to find clause start (comma, semicolon, or sentence start)
            clause_start = max(0, pos - 150)
            for i in range(pos - 1, max(0, pos - 150), -1):
                if claim_text[i] in ',;.':
                    clause_start = i + 1
                    break
            clause = claim_text[clause_start:match.end()].strip()
            if len(clause) > 20:
                return clause
    return None


def _extract_citation_rich_sections(text: str, max_chars: int = 12000) -> str:
    """
    For full papers, extract the sections that contain citations to other papers
    (typically Related Work, Introduction, Background). These are the sections
    where claims cite external sources that we can verify.
    """
    import re

    if len(text) <= max_chars:
        return text

    # Only count real bracketed citations like [1] or [1, 2, 3]
    citation_pattern = re.compile(r'\[\d+(?:,\s*\d+)*\]')
    if not citation_pattern.findall(text):
        return text[:max_chars]

    # Try to find specific sections by their headers
    # Common section headers in academic papers
    section_patterns = [
        r'(?:^|\n)\s*\d*\.?\s*(?:Related\s+Work|RELATED\s+WORK)',
        r'(?:^|\n)\s*\d*\.?\s*(?:Background|BACKGROUND)',
        r'(?:^|\n)\s*\d*\.?\s*(?:Introduction|INTRODUCTION)',
        r'(?:^|\n)\s*\d*\.?\s*(?:Literature\s+Review|LITERATURE\s+REVIEW)',
        r'(?:^|\n)\s*\d*\.?\s*(?:Previous\s+Work|PREVIOUS\s+WORK)',
    ]

    extracted_sections = []
    for pattern in section_patterns:
        match = re.search(pattern, text)
        if match:
            start = match.start()
            # Find the next section header (a line starting with a number or all-caps word)
            rest = text[start + len(match.group()):]
            next_header = re.search(r'\n\s*\d+\.?\s+[A-Z]|\n\s*[A-Z]{2,}[a-z]', rest)
            end = start + len(match.group()) + (next_header.start() if next_header else min(8000, len(rest)))
            section = text[start:end]
            if len(citation_pattern.findall(section)) >= 3:  # Must have at least 3 citations
                extracted_sections.append(section)

    if extracted_sections:
        combined = '\n\n'.join(extracted_sections)
        return combined[:max_chars]

    # Fallback: just take chunks of text that contain citations
    lines = text.split('\n')
    citation_lines = []
    for i, line in enumerate(lines):
        if citation_pattern.search(line) and len(line) > 60:
            # Include some context (2 lines before, the line itself)
            start_idx = max(0, i - 2)
            context = '\n'.join(lines[start_idx:i+1])
            citation_lines.append(context)

    if citation_lines:
        return '\n\n'.join(citation_lines)[:max_chars]

    return text[:max_chars]


def _run_verification(file_path: str, filename: str, suffix: str, source_type: str,
                      databases: list = None, custom_database: str = "") -> dict:
    """
    Run the HallucinationNerd pipeline on the uploaded file.
    This runs in a thread to not block the event loop.
    """
    from verify_hallucinations import (
        _parse_pdf,
        decompose_claims,
        verify_citations_for_question,
        run_citation_verification,
    )

    # Step 1: Extract text based on file type
    if suffix == ".pdf":
        text = _parse_pdf(file_path)
    elif suffix in (".html", ".htm"):
        from bs4 import BeautifulSoup
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
    elif suffix == ".json":
        with open(file_path, "r") as f:
            data = json.load(f)
        # If JSON matches our input schema, use it directly
        if isinstance(data, list) and data and "synopsis" in data[0]:
            return _verify_structured_input(data)
        elif isinstance(data, dict) and "synopsis" in data:
            return _verify_structured_input([data])
        else:
            text = json.dumps(data, indent=2)
    else:
        # Plain text
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()

    if not text or len(text.strip()) < 50:
        return {"error": "Could not extract sufficient text from the uploaded file."}

    # Step 2: Extract claims with citations using deterministic regex extraction
    # (more reliable than LLM-based decompose_claims for preserving citation markers)
    from pdf_claim_extractor import extract_cited_claims_from_text, split_multi_ref_claims, extract_uncited_claims_from_text

    claims = extract_cited_claims_from_text(text)

    # If the user enabled backup databases, also pull in uncited claims so they
    # are actually checked against those databases (otherwise the citation-only
    # extractor silently drops every uncited claim).
    if (databases or custom_database):
        _cited_keys = {c["claim_text"][:100] for c in claims}
        for uc in extract_uncited_claims_from_text(text):
            if uc["claim_text"][:100] not in _cited_keys:
                claims.append(uc)

    # If regex extraction found nothing, fall back to LLM-based decomposition
    if not claims:
        text_for_decomposition = _extract_citation_rich_sections(text)
        claims = decompose_claims(text_for_decomposition)

    if not claims:
        return {"error": "No verifiable claims with citations found in the document."}

    # Step 2.5: Split multi-citation sentences into atomic per-ref claims
    # This matches what the benchmark does (one claim, one source)
    claims = split_multi_ref_claims(claims)

    # Step 2.5: Resolve citations to actual source content
    from citation_resolver import resolve_and_fetch_all

    # Collect all unique cited refs across all claims
    all_cited = set()
    for claim_data in claims:
        for ref in claim_data.get("cited_refs", []):
            all_cited.add(str(ref))

    # Resolve all citations at once (fetches from arXiv, DOI, PubMed, URLs)
    # Use FULL text here (not trimmed) since References section is at the end
    resolved_sources = {}
    if all_cited:
        resolved_sources = resolve_and_fetch_all(text, list(all_cited))

    # Step 3: Verify each claim using per-ref verification (C1 fix)
    # The old code picked the first accessible ref and verified the whole
    # sentence against it, which produced PARTIALLY_SUPPORTED for multi-ref
    # claims. The new code verifies each ref independently and aggregates.
    from verify_hallucinations import verify_claim_per_ref, _find_relevant_spans

    results = []
    databases = databases or []
    for claim_data in claims:
        claim_text = claim_data.get("claim_text", claim_data.get("claim", ""))
        cited_refs = claim_data.get("cited_refs", [])

        if not cited_refs:
            # No inline citation. If the user enabled backup databases, search
            # them for supporting evidence; otherwise report it as uncited.
            if databases or custom_database:
                from citation_resolver import backup_search
                bcontent, bsource = backup_search(claim_text, databases, custom_database)
                if bcontent:
                    chunked = _find_relevant_spans(claim_text, bcontent)
                    if not chunked or len(chunked) < 50:
                        chunked = bcontent[:15000]
                    bv = verify_claim_per_ref(
                        claim={"claim_text": claim_text, "cited_refs": []},
                        articles=[{"id": "1", "content": chunked}],
                        question="",
                        question_id=hashlib.md5((claim_text + "backup").encode()).hexdigest()[:12],
                        claim_idx=1,
                        search_backup=False,
                    )
                    if bv.verdict in ("SUPPORTED", "PARTIALLY_SUPPORTED"):
                        results.append({
                            "claim": claim_text, "cited_refs": cited_refs,
                            "verdict": "BACKUP_FOUND", "confidence": bv.confidence,
                            "evidence_quote": bv.evidence_quote,
                            "reasoning": f"No inline citation. Supporting evidence found via {bsource} (verdict against that source: {bv.verdict}).",
                            "citation_exists": None, "per_ref_verdicts": [], "backup_source": bsource,
                        })
                    else:
                        results.append({
                            "claim": claim_text, "cited_refs": cited_refs,
                            "verdict": "NO_BACKUP_FOUND", "confidence": 0.0, "evidence_quote": "",
                            "reasoning": f"No inline citation. Searched {bsource}, but the top candidate did not support the claim ({bv.verdict}).",
                            "citation_exists": None, "per_ref_verdicts": [], "backup_source": bsource,
                        })
                else:
                    searched = ", ".join(databases) if databases else "custom database"
                    results.append({
                        "claim": claim_text, "cited_refs": cited_refs,
                        "verdict": "NO_BACKUP_FOUND", "confidence": 0.0, "evidence_quote": "",
                        "reasoning": f"No inline citation. No supporting source found in the selected database(s): {searched}.",
                        "citation_exists": None, "per_ref_verdicts": [],
                    })
            else:
                results.append({
                    "claim": claim_text,
                    "cited_refs": cited_refs,
                    "verdict": "UNVERIFIABLE",
                    "confidence": 0.0,
                    "evidence_quote": "",
                    "reasoning": "No citation provided for this claim.",
                    "citation_exists": None,
                    "per_ref_verdicts": [],
                })
            continue

        # Build the articles list (positional [N] -> articles[N-1]) for the engine.
        # Each cited ref becomes one article. Unresolvable refs are tracked
        # separately so the response can surface them.
        articles = []
        resolved_ref_keys = []
        unresolved_refs = []
        for ref in cited_refs:
            rk = str(ref)
            content = resolved_sources.get(rk)
            if content and len(content.strip()) > 20:
                # Chunk the source content with keyword-overlap so the LLM sees
                # the most relevant spans, not the whole (potentially 50K-char) source.
                chunked = _find_relevant_spans(claim_text, content)
                if not chunked or len(chunked) < 50:
                    chunked = content[:15000]
                articles.append({"id": rk, "content": chunked})
                resolved_ref_keys.append(ref)
            else:
                unresolved_refs.append(ref)

        # Per-ref verification: one LLM call per ref, aggregate strongest.
        # search_backup=True makes the engine dispatch to PubMed for cited-but-
        # paywalled refs and for refs whose content is otherwise unverifiable
        # (Dennis's 6 Aug directive: "HVE would find hallucinations when
        # references are given and provide references for unverifiable
        # statements").
        use_backup = bool(databases or custom_database)
        verification = verify_claim_per_ref(
            claim={"claim_text": claim_text, "cited_refs": cited_refs},
            articles=articles,
            question="",  # website path doesn't have an original question
            question_id=hashlib.md5(claim_text.encode()).hexdigest()[:12],
            claim_idx=1,
            search_backup=use_backup,
        )
        per_ref = getattr(verification, "per_ref_verdicts", [])

        # Map per-ref verdicts back to the original ref numbers (resolved_ref_keys
        # are 1-indexed, articles are 0-indexed; we just translate the article idx
        # to the original ref num).
        ref_idx_to_num = {i + 1: resolved_ref_keys[i] for i in range(len(resolved_ref_keys))}
        for entry in per_ref:
            if entry.get("ref_num") in (None, 0):
                entry["ref_num"] = ref_idx_to_num.get(entry.get("ref_num"), entry.get("ref_num"))

        # When no ref resolved at all, surface the unresolved_refs list and
        # indicate the citation couldn't be reached. The engine has already
        # dispatched to search-backup if the verdict is BACKUP_FOUND/NO_BACKUP_FOUND.
        verdict_out = verification.verdict
        if not articles and unresolved_refs:
            unresolved_list = ", ".join(str(r) for r in unresolved_refs)
            note = (
                f"A citation is present ([{unresolved_list}]), but the cited source(s) could not be "
                f"accessed (they may be behind a paywall, unavailable, or could not be resolved), "
                f"so the claim could not be verified against them."
            )
            # A citation IS present, so this is NOT "no backup source" — it is an
            # inaccessible source. Label it clearly. Only surface a backup result
            # when the user actually enabled backup databases AND one supported it.
            if use_backup and verification.verdict in ("BACKUP_FOUND", "BACKUP_PARTIAL"):
                verdict_out = verification.verdict
                reasoning = f"{note}\n{verification.reasoning}"
            else:
                verdict_out = "INACCESSIBLE"
                reasoning = note
        else:
            reasoning = verification.reasoning

        # Wrong-source guard: if content WAS resolved but is topically unrelated
        # to the claim, do not assert a confident NOT_SUPPORTED/CONTRADICTED off a
        # mis-fetched source — mark it inaccessible instead.
        if articles and verdict_out in ("NOT_SUPPORTED", "CONTRADICTED") and _looks_off_topic(claim_text, articles):
            reflist = ", ".join(str(r) for r in cited_refs)
            reasoning = (
                f"A citation is present ([{reflist}]), but the source retrieved for it did not "
                f"appear to match the citation, so the claim could not be reliably verified against it."
            )
            verdict_out = "INACCESSIBLE"

        results.append({
            "claim": claim_text,
            "cited_refs": cited_refs,
            "verdict": verdict_out,
            "confidence": verification.confidence,
            "evidence_quote": verification.evidence_quote,
            "evidence_reference": verification.evidence_reference,
            "reasoning": reasoning,
            "citation_exists": bool(articles),
            "unresolved_refs": unresolved_refs,
            "per_ref_verdicts": per_ref,
        })

    # Step 4: Compute summary
    total = len(results)
    supported = sum(1 for r in results if r["verdict"] == "SUPPORTED")
    partial = sum(1 for r in results if r["verdict"] == "PARTIALLY_SUPPORTED")
    not_supported = sum(1 for r in results if r["verdict"] == "NOT_SUPPORTED")
    contradicted = sum(1 for r in results if r["verdict"] == "CONTRADICTED")
    unverifiable = sum(1 for r in results if r["verdict"] == "UNVERIFIABLE")
    inaccessible = sum(1 for r in results if r["verdict"] == "INACCESSIBLE")
    backup_found = sum(1 for r in results if r["verdict"] == "BACKUP_FOUND")
    no_backup_found = sum(1 for r in results if r["verdict"] == "NO_BACKUP_FOUND")

    # A claim whose cited source could not be accessed is not "verifiable" — it has
    # no accessible source to check against — so exclude it from the denominator.
    verifiable = total - unverifiable - inaccessible
    reliability_pct = (supported + partial + backup_found) / verifiable * 100 if verifiable > 0 else 0
    # H5 fix: also expose the supported-only precision. PARTIALLY is
    # ambiguous (the claim is only partially supported by the cited source),
    # so reporting (supported + partial) as "reliability" overstates
    # confidence. The UI should show both.
    precision_pct = supported / verifiable * 100 if verifiable > 0 else 0

    return {
        "filename": filename,
        "summary": {
            "total_claims": total,
            "supported": supported,
            "partially_supported": partial,
            "not_supported": not_supported,
            "contradicted": contradicted,
            "unverifiable": unverifiable,
            "inaccessible": inaccessible,
            "backup_found": backup_found,
            "no_backup_found": no_backup_found,
            "reliability_percent": round(reliability_pct, 1),  # includes PARTIALLY
            "reliability_percent_full": round(reliability_pct, 6),  # L14: full precision for export
            "precision_percent": round(precision_pct, 1),  # SUPPORTED only
            "precision_percent_full": round(precision_pct, 6),
        },
        "claims": results,
    }


def _looks_off_topic(claim_text: str, articles: list, min_overlap: float = 0.15) -> bool:
    """True if the resolved source content is topically unrelated to the claim.

    Guards against a mis-fetched source (e.g. a citation that resolved to an
    unrelated paper) producing a confident NOT_SUPPORTED. Genuine
    not-supported cases share the claim's topic (high overlap); a wrong-source
    match shares almost none.
    """
    def toks(x):
        return {w for w in re.sub(r"[^\w\s]", " ", (x or "").lower()).split() if len(w) > 4}
    q = toks(claim_text)
    if not q:
        return False
    body = toks(" ".join(a.get("content", "") for a in articles))
    return (len(q & body) / len(q)) < min_overlap


def _verify_structured_input(data: list) -> dict:
    """Handle JSON input that matches our structured schema."""
    from verify_hallucinations import verify_citations_for_question

    all_results = []
    for entry in data:
        try:
            results = verify_citations_for_question(entry, single_claim=True)
            if results:
                for r in results:
                    # Handle both dataclass objects and dicts
                    if hasattr(r, 'claim_text'):
                        all_results.append({
                            "claim": r.claim_text,
                            "cited_refs": r.cited_refs,
                            "verdict": r.verdict,
                            "confidence": r.confidence,
                            "evidence_quote": r.evidence_quote,
                            "reasoning": r.reasoning,
                            "citation_exists": True,
                        })
                    else:
                        all_results.append({
                            "claim": r.get('claim_text', ''),
                            "cited_refs": r.get('cited_refs', []),
                            "verdict": r.get('verdict', 'UNVERIFIABLE'),
                            "confidence": r.get('confidence', 0.0),
                            "evidence_quote": r.get('evidence_quote', ''),
                            "reasoning": r.get('reasoning', ''),
                            "citation_exists": True,
                        })
        except Exception as e:
            all_results.append({
                "claim": entry.get("synopsis", ""),
                "cited_refs": [],
                "verdict": "UNVERIFIABLE",
                "confidence": 0.0,
                "evidence_quote": "",
                "reasoning": f"Processing error: {str(e)}",
            })

    total = len(all_results)
    supported = sum(1 for r in all_results if r["verdict"] == "SUPPORTED")
    partial = sum(1 for r in all_results if r["verdict"] == "PARTIALLY_SUPPORTED")
    not_supported = sum(1 for r in all_results if r["verdict"] == "NOT_SUPPORTED")
    contradicted = sum(1 for r in all_results if r["verdict"] == "CONTRADICTED")
    unverifiable = sum(1 for r in all_results if r["verdict"] == "UNVERIFIABLE")

    verifiable = total - unverifiable
    reliability_pct = (supported + partial) / verifiable * 100 if verifiable > 0 else 0
    precision_pct = supported / verifiable * 100 if verifiable > 0 else 0

    return {
        "filename": "structured_input.json",
        "summary": {
            "total_claims": total,
            "supported": supported,
            "partially_supported": partial,
            "not_supported": not_supported,
            "contradicted": contradicted,
            "unverifiable": unverifiable,
            "reliability_percent": round(reliability_pct, 1),  # includes PARTIALLY
            "reliability_percent_full": round(reliability_pct, 6),  # L14: full precision
            "precision_percent": round(precision_pct, 1),  # SUPPORTED only
            "precision_percent_full": round(precision_pct, 6),
        },
        "claims": all_results,
    }


if __name__ == "__main__":
    import uvicorn
    # M18: default to localhost for safety. Set HOST=0.0.0.0 to expose
    # publicly (e.g., behind a reverse proxy).
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host=host, port=port)
