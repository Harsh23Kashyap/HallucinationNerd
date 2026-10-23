"""
Generic ArXiv Citation Extractor
=================================
Extracts (claim, cited paper) pairs from any arXiv paper's Related Work section.

Supports ALL common academic citation formats:
1. (Author et al., 2020)     — parenthetical author-year (ACL/NeurIPS)
2. [N] or [N, M]             — numbered brackets (IEEE/CVPR), incl. ranges [3-7]
3. [Author et al., 2020]     — bracketed author-year
4. Author et al. (2020)      — narrative author-year
5. (N) or (N, M)             — parenthetical numbers
6. [Key17] / [Key+17]        — alphanumeric labels (alpha/BibTeX-key style)

Hardening (verifier-reliability fix):
- Page headers/footers are stripped before parsing (PyMuPDF returns them
  inline; a running head repeating the section title otherwise confuses
  section detection and footer lines corrupt bibliography entries).
- Section detection anchors on heading-like lines, so a mention of
  "related work" in ordinary prose is not mistaken for the section.
- Every number in a multi-citation [N, M] is paired with the clause
  (previously only the first), and the most informative clause wins per ref.
- Sentence/clause boundaries protect abbreviation periods (et al., Fig.,
  i.e., e.g., ...), so clauses are not truncated mid-citation.
- Mixed-format papers run every detected format and combine the pairs.
- Detached bibliography labels ("12." at line start, label on its own line)
  are parsed.

Usage:
    from arxiv_extractor import extract_citations_from_pdf
    claims = extract_citations_from_pdf("paper.pdf", section="related work")
    # Returns: [{"clause": "...", "arxiv_id": "2305.14314", ...}, ...]
"""

import re
import fitz  # PyMuPDF


# ─── Abbreviation period protection ─────────────────────────────────────────
# Sentence boundary detection must not split at "et al.", "Fig.", "i.e.",
# etc. Replace the period with a placeholder before boundary search, then
# restore. Same approach as web/pdf_claim_extractor.py (H2 fix).
_ABBREV_RE = re.compile(
    r"\b(et al|Fig|Figs|Sec|Secs|Sect|Eq|Eqs|i\.e|e\.g|cf|vs|Dr|Mr|Ms|Prof|Sr|Jr|No|Nos|pp|Vol|Eds?|Inc|Ltd|St)\."
)
_ABBR_PH = "\x00ABBR\x00"



_ARXIV_ID_RE = re.compile(r'(\d{4}\.\d{4,5})')
_ARXIV_EXPLICIT_RE = re.compile(r'(?:arXiv:|arxiv\.org/(?:abs|pdf)/)\s*(\d{4}\.\d{4,5})', re.I)

def _valid_arxiv_id(candidate):
    """YYMM.NNNNN arXiv id sanity: month 01-12 (rejects DOI suffixes, years)."""
    if not candidate:
        return False
    ym = candidate.split(".", 1)[0]
    if len(ym) != 4 or not ym.isdigit():
        return False
    month = int(ym[2:])
    return 1 <= month <= 12

def _arxiv_id_from_entry(entry):
    """First arXiv id in a bibliography entry; explicit arXiv markers win.
    Bare numbers must pass the month check and must not be glued to a
    longer token (rejects DOI suffixes like 10.1109/MSR.2012.12345)."""
    explicit = _ARXIV_EXPLICIT_RE.search(entry)
    if explicit and _valid_arxiv_id(explicit.group(1)):
        return explicit.group(1)
    for m in _ARXIV_ID_RE.finditer(entry):
        if not _valid_arxiv_id(m.group(1)):
            continue
        if m.start() > 0 and entry[m.start() - 1] in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789./-":
            continue
        return m.group(1)
    return None

def _protect_abbrev_periods(text):
    return _ABBREV_RE.sub(lambda m: m.group(0)[:-1] + _ABBR_PH, text)


def _restore_abbrev_periods(text):
    return text.replace(_ABBR_PH, ".")


# ─── Page artifact stripping ────────────────────────────────────────────────

def _page_texts(pdf_path):
    doc = fitz.open(pdf_path)
    pages = [page.get_text() for page in doc]
    doc.close()
    return pages


def _strip_page_artifacts(pages):
    """Remove running heads/footers and bare page numbers from per-page text.

    A line that appears on at least half of all pages (documents of 3+
    pages) is treated as a running head/footer. Bare page-number lines are
    always dropped. Returns the cleaned full text.
    """
    if not pages:
        return ""
    line_pages = {}
    for i, p in enumerate(pages):
        for ln in set(l.strip() for l in p.split("\n") if l.strip()):
            line_pages.setdefault(ln, set()).add(i)
    n = len(pages)

    def _artifact(ln):
        if not ln:
            return False
        if re.fullmatch(r"\d{1,4}", ln):
            return True
        return n >= 3 and len(line_pages.get(ln, ())) >= max(2, (n + 1) // 2)

    cleaned = []
    for p in pages:
        kept = [l for l in p.split("\n") if not _artifact(l.strip())]
        cleaned.append("\n".join(kept))
    return "\n".join(cleaned)


# ─── Section location ───────────────────────────────────────────────────────

_SECTION_HEADING_RE = re.compile(
    r"^[ \t]*(?:\d+(?:\.\d+)*\.?[ \t]+|[IVXLC]+\.?[ \t]+)?"
    r"(related work|background|related works)\b[^\n]{0,50}$",
    re.IGNORECASE | re.MULTILINE,
)

_NEXT_HEADING_RE = re.compile(
    r"^[ \t]*\d+(?:\.\d+)*\.?[ \t]+[A-Z]", re.MULTILINE
)


def _find_section_start(text, section):
    """Locate a section by heading-like line first; fall back to substring.

    Heading anchoring prevents a prose mention (e.g. "we survey related work
    in the appendix") from being mistaken for the section itself.
    """
    wanted = section.lower()
    aliases = {wanted}
    if wanted == "related work":
        aliases.add("related works")
        aliases.add("background")
    for m in _SECTION_HEADING_RE.finditer(text):
        if m.group(1).lower() in aliases:
            return m.start()
    idx = text.lower().find(section)
    if idx == -1 and section == "related work":
        idx = text.lower().find("background")
    return idx


# ─── Citation format detection ──────────────────────────────────────────────

_ALPHA_KEY = r"[A-Z][A-Za-z]*(?:\+| et al\.)?\d{2}[a-z]?"


def detect_citation_format(text):
    """Detect citation format(s) used in text. Returns sorted by count (most common first)."""
    formats = []

    # Format 1: (Author et al., Year)
    f1 = re.findall(r'\(([A-Z][\w\'\-]+(?:\s*et al\.)?),?\s*\d{4}[a-z]?\)', text)
    if f1:
        formats.append(("author_year_paren", len(f1)))

    # Format 3: [Author et al., Year]
    f3 = re.findall(r'\[([A-Z][\w\'\-]+(?:\s*et al\.)?),?\s*\d{4}[a-z]?\]', text)
    if f3:
        formats.append(("author_year_bracket", len(f3)))

    # Format 4: Author et al. (Year) — narrative
    f4 = re.findall(r'([A-Z][\w\'\-]+(?:\s*et al\.)?)\s*\(\d{4}[a-z]?\)', text)
    if f4:
        formats.append(("author_year_narrative", len(f4)))

    # Format 2: [N] or [N, M] — counted even alongside author-year brackets
    # (mixed-format papers exist; every detected format is extracted).
    f2 = re.findall(r'\[(\d+(?:[,\s\-–]\s*\d+)*)\]', text)
    if f2:
        formats.append(("numbered_bracket", len(f2)))

    # Format 6: [Key17] / [Key+17] — alphanumeric labels
    f6 = re.findall(r'\[(' + _ALPHA_KEY + r')\]', text)
    if f6:
        formats.append(("alpha_bracket", len(f6)))

    # Format 5: (N) — only if no author-year parens and numbers are small
    if not f1 and not f4:
        f5 = re.findall(r'\((\d+(?:,\s*\d+)*)\)', text)
        if f5:
            formats.append(("numbered_paren", len(f5)))

    formats.sort(key=lambda x: x[1], reverse=True)
    return formats


# ─── Main entry point ───────────────────────────────────────────────────────

def extract_citations_from_pdf(pdf_path, section="related work"):
    """
    Extract (claim, arxiv_id) pairs from a paper's specified section.

    Args:
        pdf_path: Path to PDF file
        section: Section to extract from (default: "related work")

    Returns:
        List of dicts with keys: clause, arxiv_id, and format-specific keys
    """
    text = _strip_page_artifacts(_page_texts(pdf_path))

    # Find section
    section_start = _find_section_start(text, section)
    if section_start == -1:
        return []

    # Find end of section: next numbered heading, else a bounded window
    text_after = text[section_start + 50:]
    next_heading = _NEXT_HEADING_RE.search(text_after)
    if next_heading:
        section_end = section_start + 50 + next_heading.start()
    else:
        legacy = re.search(r'\n\d+\.?\s+[A-Z]', text_after)
        section_end = section_start + 50 + (legacy.start() if legacy else 5000)
    section_text = text[section_start:section_end].replace('\n', ' ')

    # Get bibliography
    ref_start = text.lower().rfind("\nreferences")
    bibliography = text[ref_start:] if ref_start != -1 else ""

    # Detect formats and run every detected one (mixed-format papers)
    formats = detect_citation_format(section_text)
    if not formats:
        return []

    results = []
    seen_pairs = set()
    for fmt, _count in formats:
        if fmt in ("numbered_bracket", "numbered_paren"):
            pairs = _extract_numbered(section_text, bibliography, fmt)
        elif fmt == "alpha_bracket":
            pairs = _extract_alpha(section_text, bibliography)
        else:
            pairs = _extract_author_year(section_text, bibliography, fmt)
        for p in pairs:
            key = (p["arxiv_id"], p["clause"][:80])
            if key not in seen_pairs:
                seen_pairs.add(key)
                results.append(p)
    return results


# ─── Numbered formats ───────────────────────────────────────────────────────

def _expand_nums(group):
    """Expand "5, 7" and ranges like "3-7" into individual numbers."""
    nums = []
    for part in re.split(r"\s*,\s*", group.strip()):
        m = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < a <= b <= a + 50:
                nums.extend(range(a, b + 1))
            continue
        if part.isdigit():
            nums.append(int(part))
    return nums


def _extract_numbered(section_text, bibliography, fmt):
    """Extract from numbered citation formats [N] or (N)."""
    if fmt == "numbered_bracket":
        num_pattern = r'\[([\d,\s\-–]+)\]'
        cite_pattern = r'\[[\d,\s\-–]+\]'
    else:
        num_pattern = r'\(([\d,\s]+)\)'
        cite_pattern = r'\([\d,\s]+\)'

    bib_entries = _parse_numbered_bibliography(bibliography)

    # Pair EVERY number in a multi-citation with the clause (previously only
    # the first number was kept, dropping the rest of the mapping). When a
    # ref is cited by several clauses, keep the most informative (longest).
    best_clause = {}
    for m in re.finditer(num_pattern, section_text):
        nums = _expand_nums(m.group(1))
        if not nums:
            continue
        clause = _get_clause_before(section_text, m.start(), cite_pattern)
        if len(clause) < 20:
            continue
        for num in nums:
            if num not in bib_entries:
                continue
            if num not in best_clause or len(clause) > len(best_clause[num]):
                best_clause[num] = clause

    results = []
    for num in sorted(best_clause):
        arxiv_id = _arxiv_id_from_entry(bib_entries[num])
        if arxiv_id:
            results.append({
                "clause": best_clause[num],
                "arxiv_id": arxiv_id,
                "cite_num": num,
                "bib_entry": bib_entries[num][:100],
                "format": "numbered",
            })
    return results


def _parse_numbered_bibliography(bibliography):
    """Parse a numbered bibliography, including detached labels.

    Handles "[12] Entry" (label and text possibly on separate lines) and the
    line-anchored "12. Entry" style.
    """
    bib_entries = {}
    for m in re.finditer(r'\[(\d+)\]\s*(.+?)(?=\[\d+\]|\Z)', bibliography, re.DOTALL):
        bib_entries[int(m.group(1))] = m.group(2).strip().replace('\n', ' ')

    if not bib_entries:
        # Line-anchored "12. Entry" (label may sit on its own line)
        for m in re.finditer(r'(?m)^\s*(\d+)\.\s*(.+?)(?=^\s*\d+\.\s|\Z)', bibliography, re.DOTALL):
            entry = m.group(2).strip().replace('\n', ' ')
            if entry:
                bib_entries[int(m.group(1))] = entry
    return bib_entries


# ─── Alphanumeric labels ────────────────────────────────────────────────────

def _extract_alpha(section_text, bibliography):
    """Extract from alpha-style labels: [Vaswani+17], [Devlin18]."""
    key_pattern = r'\[(' + _ALPHA_KEY + r')\]'
    cite_pattern = r'\[' + _ALPHA_KEY + r'\]'

    bib_entries = {}
    bib_re = (r'\[(' + _ALPHA_KEY + r')\]\s*(.+?)(?=\[' + _ALPHA_KEY + r'\]|\Z)')
    for m in re.finditer(bib_re, bibliography, re.DOTALL):
        bib_entries[m.group(1)] = m.group(2).strip().replace('\n', ' ')
    if not bib_entries:
        return []

    best_clause = {}
    for m in re.finditer(key_pattern, section_text):
        key = m.group(1)
        if key not in bib_entries:
            continue
        clause = _get_clause_before(section_text, m.start(), cite_pattern)
        if len(clause) < 20:
            continue
        if key not in best_clause or len(clause) > len(best_clause[key]):
            best_clause[key] = clause

    results = []
    for key in sorted(best_clause):
        arxiv_id = _arxiv_id_from_entry(bib_entries[key])
        if arxiv_id:
            results.append({
                "clause": best_clause[key],
                "arxiv_id": arxiv_id,
                "cite_key": key,
                "bib_entry": bib_entries[key][:100],
                "format": "alpha",
            })
    return results


# ─── Author-year formats ────────────────────────────────────────────────────

def _extract_author_year(section_text, bibliography, fmt):
    """Extract from author-year citation formats."""
    if fmt == "author_year_paren":
        pattern = r'\(([A-Z][\w\'\-]+(?:\s*et al\.)?),?\s*(\d{4})[a-z]?\)'
        cite_pattern = r'\([A-Z][\w\'\-]+.*?\d{4}[a-z]?\)'
    elif fmt == "author_year_bracket":
        pattern = r'\[([A-Z][\w\'\-]+(?:\s*et al\.)?),?\s*(\d{4})[a-z]?\]'
        cite_pattern = r'\[[A-Z][\w\'\-]+.*?\d{4}[a-z]?\]'
    elif fmt == "author_year_narrative":
        pattern = r'([A-Z][\w\'\-]+(?:\s*et al\.)?)\s*\((\d{4})[a-z]?\)'
        cite_pattern = r'[A-Z][\w\'\-]+(?:\s*et al\.)?\s*\(\d{4}[a-z]?\)'
    else:
        return []

    bib_entries = re.split(r'\n(?=[A-Z][\w\'\-]+)', bibliography[20:])

    results = []
    seen = set()

    for m in re.finditer(pattern, section_text):
        author = m.group(1).replace(' et al.', '').strip()
        year = m.group(2)

        if (author, year) in seen:
            continue
        seen.add((author, year))

        # Get clause
        if fmt == "author_year_narrative":
            # For narrative, the claim comes AFTER the citation
            start_pos = m.end()
            next_cite = re.search(cite_pattern, section_text[start_pos:])
            protected = _protect_abbrev_periods(section_text)
            sent_end = protected.find('.', start_pos)
            end_pos = min(
                start_pos + (next_cite.start() if next_cite else 300),
                sent_end + 1 if sent_end != -1 else start_pos + 300
            )
            clause = _restore_abbrev_periods(
                f"{m.group(0)} {section_text[start_pos:end_pos].strip()}"
            )
        else:
            clause = _get_clause_before(section_text, m.start(), cite_pattern)

        if len(clause) < 20:
            continue

        # Match to bibliography
        arxiv_id = _find_in_bibliography(author, year, bib_entries, claim_text=clause)
        if arxiv_id:
            results.append({
                "clause": clause,
                "arxiv_id": arxiv_id,
                "author": author,
                "year": year,
                "format": fmt,
            })

    return results


# ─── Clause extraction ──────────────────────────────────────────────────────

def _get_clause_before(text, end_pos, cite_pattern):
    """Get the clause immediately before a citation position.

    Abbreviation periods (et al., Fig., i.e., ...) are protected before the
    sentence-boundary search so the clause is not truncated mid-citation.
    """
    protected = _protect_abbrev_periods(text)
    prev_cite_end = 0
    for prev_m in re.finditer(cite_pattern, protected[:end_pos]):
        prev_cite_end = prev_m.end()
    sent_start = protected.rfind('.', max(0, end_pos - 300), end_pos)
    clause_start = max(prev_cite_end, sent_start + 1) if sent_start > prev_cite_end else prev_cite_end
    return _restore_abbrev_periods(text[clause_start:end_pos]).strip().strip(',;').strip()


# ─── Bibliography matching (author-year) ────────────────────────────────────

def _find_in_bibliography(author, year, bib_entries, claim_text=""):
    """Find arXiv ID for an author+year in bibliography entries.

    Improved matching:
    1. Author name must START the entry (not just appear somewhere)
    2. Year must appear within 200 chars of start
    3. If multiple matches, use claim keywords to disambiguate
    """
    author_lower = author.lower().strip()
    if not author_lower:
        return None

    candidates = []

    for entry in bib_entries:
        entry_lower = entry.lower().strip()

        # Author must START the entry (first word match)
        entry_first_word = entry_lower.split(',')[0].split('.')[0].strip()

        # Check if author last name matches the start of the entry
        if not (entry_first_word.startswith(author_lower) or
                entry_lower.startswith(author_lower)):
            continue

        # Year must appear within first 200 chars (near the author)
        if year not in entry[:200]:
            continue

        # Has arXiv ID?
        arxiv_id = _arxiv_id_from_entry(entry)
        if not arxiv_id:
            continue

        candidates.append({
            "arxiv_id": arxiv_id,
            "entry": entry,
        })

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]["arxiv_id"]

    # Multiple matches — disambiguate using claim keywords
    if claim_text:
        claim_words = set(w.lower() for w in claim_text.split() if len(w) > 4)
        best_score = -1
        best_id = candidates[0]["arxiv_id"]

        for cand in candidates:
            entry_words = set(w.lower() for w in cand["entry"].split() if len(w) > 4)
            overlap = len(claim_words & entry_words)
            if overlap > best_score:
                best_score = overlap
                best_id = cand["arxiv_id"]

        return best_id

    # Default to first match
    return candidates[0]["arxiv_id"]


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python arxiv_extractor.py <paper.pdf> [section]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    section = sys.argv[2] if len(sys.argv) > 2 else "related work"

    results = extract_citations_from_pdf(pdf_path, section)
    print(f"Extracted {len(results)} citation-claim pairs:")
    for r in results:
        print(f"  [{r.get('cite_num', r.get('author', r.get('cite_key','')))}] \"{r['clause'][:60]}\"")
        print(f"    → arXiv:{r['arxiv_id']}")
