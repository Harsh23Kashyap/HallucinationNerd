"""
PDF Claim Extractor — deterministic, regex-based extraction of cited claims from PDFs.

Unlike decompose_claims (LLM-based, strips markers) or arxiv_extractor (requires arXiv IDs),
this module simply extracts every sentence that contains a [N] citation marker, preserving
the marker and its reference number. This is what feeds into the verification pipeline
on the website.

Produces the same format as decompose_claims: [{"claim_text": "...", "cited_refs": [N, M]}]
"""

import re
from typing import List


def _strip_leading_heading(text: str) -> str:
    """Remove a leading section number like '3 ' or '3.1 ' that PDF extraction
    sometimes glues onto the start of a claim (e.g. '3 Model Architecture ...')."""
    return re.sub(r'^\s*\d+(?:\.\d+)*\s+', '', text).strip()


def extract_uncited_claims_from_text(text: str, max_claims: int = 25) -> List[dict]:
    """Extract substantive sentences that carry NO citation marker.

    The main extractor is citation-only, so uncited claims never enter the
    pipeline — which means the advertised "backup search for uncited claims"
    feature silently drops them. When the user enables backup databases, we call
    this so those claims are actually checked. Filtered + capped to avoid
    flooding results with every sentence of a long document.
    """
    ref_start = _find_references_start(text)
    body = text[:ref_start] if ref_start else text

    sentence_splitter = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')
    sentences = sentence_splitter.split(body)

    claims = []
    seen = set()
    for sent in sentences:
        clean = re.sub(r'\s+', ' ', sent).strip()
        clean = _strip_leading_heading(clean)
        if not (40 <= len(clean) <= 400):
            continue
        if re.search(r'\[\d', clean):          # has a citation -> not uncited
            continue
        if not re.search(r'[a-z]', clean):      # skip ALL-CAPS headings
            continue
        if not clean.rstrip().endswith(('.', '!', '?')):  # skip fragments/headings
            continue
        key = clean[:100]
        if key in seen:
            continue
        seen.add(key)
        claims.append({"claim_text": clean, "cited_refs": []})
        if len(claims) >= max_claims:
            break
    return claims


def extract_cited_claims_from_text(text: str, max_claims: int = 200) -> List[dict]:
    """
    Extract sentences/clauses containing citation markers [N] from document text.
    Returns claims in the same format as decompose_claims.
    
    Works with all numbered citation formats: [1], [1,2], [1, 2, 3], [1-3]
    """
    # Find the main body (skip references section at the end)
    ref_start = _find_references_start(text)
    body = text[:ref_start] if ref_start else text

    # Citation pattern: [1] or [1, 2] or [1,2,3] or [1-3]
    cite_pattern = re.compile(r'\[(\d+(?:[\s,\-]+\d+)*)\]')

    # H2 fix: protect common academic abbreviations from being treated as
    # sentence boundaries. The naive `(?<=[.!?])\s+(?=[A-Z\[])` would
    # incorrectly split at "et al.", "Fig.", "i.e.", "e.g.", etc.
    _ABBREVIATIONS = (
        "et al", "Fig", "Eq", "i.e", "e.g", "cf", "vs", "approx",
        "Dr", "Mr", "Mrs", "Ms", "Prof", "St", "No", "Inc", "Ltd",
        "Co", "Corp", "al", "pp", "Vol",
    )
    # Use a placeholder that contains a non-period char so the splitter
    # doesn't see "<placeholder>." as a sentence boundary either.
    _ABBREV_PLACEHOLDER = "\x00ABBR\x00"

    body_protected = body
    for abbr in _ABBREVIATIONS:
        # Replace `<abbr>.` with `<abbr><placeholder>` (the period is
        # removed, not kept), so the splitter skips this as a boundary.
        body_protected = re.sub(
            r"(\b" + re.escape(abbr) + r")\.",
            r"\1" + _ABBREV_PLACEHOLDER,
            body_protected,
        )

    sentence_splitter = re.compile(r'(?<=[.!?])\s+(?=[A-Z\[])')
    sentences = sentence_splitter.split(body_protected)
    # Restore the period for each abbreviation occurrence
    sentences = [s.replace(_ABBREV_PLACEHOLDER, ".") for s in sentences]

    claims = []
    seen = set()  # Avoid duplicate claims

    for sent in sentences:
        sent = sent.strip()
        if not sent or len(sent) < 30:
            continue

        # Find all citation markers in this sentence
        matches = cite_pattern.findall(sent)
        if not matches:
            continue

        # Parse citation numbers
        cited_refs = []
        for match in matches:
            # Handle ranges like "1-3" -> [1,2,3]
            if '-' in match:
                parts = match.split('-')
                try:
                    start, end = int(parts[0].strip()), int(parts[1].strip())
                    cited_refs.extend(range(start, end + 1))
                except ValueError:
                    pass
            else:
                # Handle comma-separated: "1, 2, 3"
                for num in match.split(','):
                    num = num.strip()
                    if num.isdigit():
                        cited_refs.append(int(num))

        if not cited_refs:
            continue

        # Clean the sentence (remove line breaks from PDF extraction)
        clean = re.sub(r'\s+', ' ', sent).strip()
        clean = _strip_leading_heading(clean)
        # Remove very long sentences (likely parsing errors)
        if len(clean) > 500:
            # Try to extract just the clause around the citation
            clean = _extract_clause_around_citation(clean, cite_pattern)
            if not clean:
                continue

        # Deduplicate
        key = clean[:100]
        if key in seen:
            continue
        seen.add(key)

        claims.append({
            "claim_text": clean,
            "cited_refs": sorted(set(cited_refs)),
        })

        if len(claims) >= max_claims:
            # H3: log a warning so silent truncation is visible in dev.
            import sys
            print(
                f"  [WARN] claim extraction truncated at {max_claims} claims; "
                f"raise max_claims parameter if you need more.",
                file=sys.stderr,
            )
            break

    return claims


def _extract_clause_around_citation(text: str, cite_pattern) -> str:
    """Extract the clause containing the citation from a long sentence."""
    match = cite_pattern.search(text)
    if not match:
        return ""

    # Take ~200 chars around the citation
    start = max(0, match.start() - 150)
    end = min(len(text), match.end() + 50)

    # Extend to sentence boundaries
    while start > 0 and text[start] not in '.!?;':
        start -= 1
    if start > 0:
        start += 2  # skip the period and space

    clause = text[start:end].strip()
    return clause if len(clause) > 30 else ""


def _find_references_start(text: str) -> int:
    """Find where the References/Bibliography section starts."""
    patterns = [
        r'\n\s*References?\s*\n',
        r'\n\s*REFERENCES?\s*\n',
        r'\n\s*Bibliography\s*\n',
    ]
    for p in patterns:
        match = re.search(p, text)
        if match:
            return match.start()

    # Fallback: find last occurrence of [1] at start of line (typical bib entry)
    matches = list(re.finditer(r'\n\s*\[1\]\s+[A-Z]', text))
    if matches:
        return matches[-1].start()

    return len(text)


def split_multi_ref_claims(claims: List[dict], max_refs_per_claim: int = 3) -> List[dict]:
    """
    Split claims with many citations into smaller per-citation-group claims.
    
    A claim like "methods use attention [62,68], adapters [14,23], noise [26,27]"
    becomes 3 claims, each with the clause relevant to its citation group.
    
    Claims with <= max_refs_per_claim citations are left unchanged.
    """
    import re
    
    result = []
    cite_pattern = re.compile(r'\[(\d+(?:[\s,]+\d+)*)\]')
    
    for claim in claims:
        refs = claim.get("cited_refs", [])
        text = claim.get("claim_text", "")
        
        # If few refs, keep as-is
        if len(refs) <= max_refs_per_claim:
            result.append(claim)
            continue
        
        # Find all citation positions in the text
        matches = list(cite_pattern.finditer(text))
        if len(matches) <= 1:
            # Only one citation bracket (just many numbers in it) — keep as-is
            result.append(claim)
            continue
        
        # Try to split into per-bracket clauses. If ANY resulting clause is too
        # short (a coherent phrase like "long short-term memory [13]" being torn
        # at a comma), abandon the split and keep the whole sentence — per-ref
        # verification still checks each ref, but against full-context text
        # instead of a meaningless fragment.
        pieces = []
        fragmented = False
        for match in matches:
            pos = match.start()
            clause_start = max(0, pos - 200)
            for i in range(pos - 1, max(0, pos - 200), -1):
                if text[i] in ',;.':
                    clause_start = i + 1
                    break

            clause = text[clause_start:match.end()].strip()

            bracket_refs = []
            for num in match.group(1).split(','):
                num = num.strip()
                if num.isdigit():
                    bracket_refs.append(int(num))

            if len(clause) < 40 or not bracket_refs:
                fragmented = True
                break
            pieces.append({"claim_text": clause, "cited_refs": bracket_refs})

        if pieces and not fragmented:
            result.extend(pieces)
        else:
            result.append(claim)  # keep whole rather than emit fragments

    return result
