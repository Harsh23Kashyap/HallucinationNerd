"""
Citation Resolver — bridges citation markers [1], [2] etc. to actual source content.

Given a document's text and its reference list, this module:
1. Parses the references section to extract bibliographic entries
2. For each citation marker found in claims, resolves it to a source
3. Fetches content from the resolved source (arXiv, DOI, PubMed, URL)
4. Returns the content for verification

Supports: arXiv IDs, DOIs, PubMed IDs, URLs, and title-based search as fallback.
"""

import re
import os
import time
import hashlib
import requests
import threading
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

# Rate limiting for API calls
_last_request_time = 0
_MIN_REQUEST_INTERVAL = 0.3  # Faster with API key (100/sec allowed)

# Semantic Scholar API key (free, 100 req/sec vs 1 req/sec without)
_S2_API_KEY = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")

# Simple in-memory cache for resolved sources. Entries expire after
# `_CACHE_TTL_SECONDS` so stale arXiv versions and rate-limited Unpaywall
# responses don't stick forever.
import time as _time
_source_cache: dict = {}  # cache_key -> (timestamp, content)
_CACHE_TTL_SECONDS = 3600  # 1 hour default; override with HVE_CACHE_TTL


def _rate_limit():
    """Simple rate limiting to be polite to external APIs."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.time()


_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_DEFAULT_REQUEST_ATTEMPTS = 2  # initial request + one backoff retry
_PATIENT_REQUEST_ATTEMPTS = 11  # initial request + up to ten backoff retries
_MAX_RETRY_DELAY_SECONDS = 5.0
_request_policy = threading.local()


def _retry_delay(response, attempt: int) -> float:
    """Return a bounded Retry-After delay or an exponential fallback."""
    retry_after = ""
    if response is not None:
        retry_after = (getattr(response, "headers", {}) or {}).get("Retry-After", "")
    try:
        delay = float(retry_after)
    except (TypeError, ValueError):
        delay = 0.6 * (2 ** attempt)
    return min(max(delay, 0.0), _MAX_RETRY_DELAY_SECONDS)


def _get_with_backoff(url: str, **kwargs):
    """GET with the active bounded retry policy.

    Normal verification makes one backoff retry. A user who chooses "I can
    wait" gets up to ten backoff retries. The policy is thread-local because
    citation downloads run in a worker pool.
    """
    response = None
    attempts = getattr(_request_policy, "attempts", _DEFAULT_REQUEST_ATTEMPTS)
    for attempt in range(attempts):
        _rate_limit()
        try:
            response = requests.get(url, **kwargs)
        except requests.RequestException:
            response = None

        status = getattr(response, "status_code", None)
        if response is not None and status not in _RETRYABLE_STATUS_CODES:
            _request_policy.throttled = False
            return response
        if attempt + 1 < attempts:
            _time.sleep(_retry_delay(response, attempt))
    if getattr(response, "status_code", None) == 429:
        _request_policy.throttled = True
    return response


def resolve_references(full_text: str) -> dict:
    """
    Parse the references/bibliography section from a document and return
    a dict mapping reference number/key to bibliographic info.
    
    Returns: {
        "1": {"title": "...", "authors": "...", "year": "...", "arxiv_id": "...", "doi": "...", "url": "..."},
        "2": {...},
        ...
    }
    """
    refs = {}

    # Try to find the References/Bibliography section
    ref_section = _extract_reference_section(full_text)
    if not ref_section:
        return refs

    # Parse numbered references: [1] Author... or 1. Author...
    numbered = re.findall(
        r'(?:^\[(\d+)\]|\n\[(\d+)\]|\n(\d+)\.)\s*(.+?)(?=(?:\n\[\d+\]|\n\d+\.|\Z))',
        ref_section, re.DOTALL
    )

    for match in numbered:
        num = match[0] or match[1] or match[2]
        entry_text = match[3].strip()
        refs[num] = _parse_single_reference(entry_text)

    # Validate the numbered parse. PDF text is full of spurious "\\n12." hits
    # (years, table rows, page numbers) that produce junk keys like "1543" or
    # fragment a real key ("[10]" -> key "1" + text "0: ..."). Real numbered
    # bibliographies have plausible key ranges and year/identifier-bearing
    # entries; junk parses do not.
    if refs:
        plausible = {k: v for k, v in refs.items()
                     if k.isdigit() and 1 <= int(k) <= 400}
        bib_like = [k for k, v in plausible.items() if _looks_like_bib_entry(v.get("raw", ""))]
        if not plausible or len(bib_like) < max(2, 0.5 * len(plausible)):
            refs = {}  # junk parse — author-year parsing below takes over
        else:
            refs = plausible

    # Author-year (unnumbered) bibliographies: parse alongside and merge.
    # Keys are "surnameYEAR" so they never collide with numeric keys.
    ay_refs = _parse_author_year_refs(ref_section)
    for k, v in ay_refs.items():
        refs.setdefault(k, v)

    # Last resort: unnumbered list, one entry per line
    if not refs:
        lines = [l.strip() for l in ref_section.split('\n') if l.strip() and len(l.strip()) > 20]
        for i, line in enumerate(lines[:50], 1):  # cap at 50 refs
            refs[str(i)] = _parse_single_reference(line)

    return refs


# Trailing sections that can follow the bibliography (esp. in revised
# submissions) and carry their own numbered lists, which would otherwise
# overwrite real references. The bibliography is truncated before these.
_AY_POST_BIB = re.compile(
    r"(?:^|\n)\s*(?:"
    r"Response\s+to\s+(?:the\s+)?reviewers?"
    r"|Reviewer\s+(?:response|comments?)"
    r"|Author\s+(?:biograph|contributions)"
    r"|Response\s+letter"
    r"|Rebuttal"
    r"|Cover\s+letter"
    r")",
    re.IGNORECASE,
)


def _truncate_post_bib(section: str) -> str:
    """Cut a bibliography section at the start of any trailing non-bib section
    (e.g. a revision's "Response to reviewers"), whose own [1],[2]... would
    otherwise overwrite the real references."""
    m = _AY_POST_BIB.search(section)
    return section[:m.start()] if m else section


def _extract_reference_section(text: str) -> Optional[str]:
    """Extract the references/bibliography section from document text."""
    # Common section headers
    patterns = [
        r'(?:^|\n)\s*References?\s*\n',
        r'(?:^|\n)\s*REFERENCES?\s*\n',
        r'(?:^|\n)\s*Bibliography\s*\n',
        r'(?:^|\n)\s*BIBLIOGRAPHY\s*\n',
        r'(?:^|\n)\s*Works Cited\s*\n',
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _truncate_post_bib(text[match.end():])

    # Fallback: look for the last section that starts with [1] or 1.
    match = re.search(r'\n\s*\[1\]\s+\w', text)
    if match:
        return _truncate_post_bib(text[match.start():])

    return None


def _parse_single_reference(entry_text: str) -> dict:
    """Extract structured info from a single reference entry."""
    # Normalize line breaks (PDF extraction inserts them mid-sentence)
    entry_text = re.sub(r'\n\s*', ' ', entry_text)
    
    info = {
        "raw": entry_text[:500],
        "title": "",
        "authors": "",
        "year": "",
        "arxiv_id": "",
        "doi": "",
        "url": "",
        "pmid": "",
    }

    # Extract arXiv ID — handle "arXiv:1706.03762", "arXiv preprint arXiv:...",
    # and the very common "CoRR, abs/1409.0473" / "abs/1409.0473" forms, plus a
    # trailing version suffix ("1706.03762v5").
    arxiv_match = re.search(r'(?:arXiv[:\s]*|abs/)(\d{4}\.\d{4,5})(?:v\d+)?', entry_text, re.IGNORECASE)
    if arxiv_match:
        info["arxiv_id"] = arxiv_match.group(1)

    # Extract DOI
    doi_match = re.search(r'(10\.\d{4,}/[^\s,;]+)', entry_text)
    if doi_match:
        info["doi"] = doi_match.group(1).rstrip('.')

    # Extract URL (skip bare arxiv.org/abs URLs — the arXiv ID above handles those)
    url_match = re.search(r'(https?://[^\s,;>]+)', entry_text)
    if url_match:
        info["url"] = url_match.group(1).rstrip('.')

    # Extract PubMed ID
    pmid_match = re.search(r'PMID[:\s]*(\d+)', entry_text, re.IGNORECASE)
    if pmid_match:
        info["pmid"] = pmid_match.group(1)

    # Extract year
    year_match = re.search(r'\((\d{4})\)|,\s*(\d{4})', entry_text)
    if year_match:
        info["year"] = year_match.group(1) or year_match.group(2)

    # Extract title. Build a cleaned string first: strip identifiers and venue
    # tails so the title heuristic doesn't grab the arXiv number (the old bug
    # produced titles like "1607.06450, 2016.").
    clean = entry_text
    clean = re.sub(r'arXiv[:\s]*\d{4}\.\d{4,5}(?:v\d+)?', ' ', clean, flags=re.IGNORECASE)
    clean = re.sub(r'\barXiv\s+preprint\b', ' ', clean, flags=re.IGNORECASE)
    clean = re.sub(r'\bCoRR\b.*', ' ', clean)              # drop CoRR venue tail
    clean = re.sub(r'\babs/\d{4}\.\d{4,5}', ' ', clean)
    clean = re.sub(r'https?://\S+', ' ', clean)
    clean = re.sub(r'10\.\d{4,}/\S+', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()

    quoted = re.search(r'["\u201c\u201d\u2018\u2019](.+?)["\u201c\u201d\u2018\u2019]', entry_text)
    if quoted and len(quoted.group(1)) > 6:
        info["title"] = quoted.group(1).strip()
    else:
        # CS format: "Authors. Title. Venue, Year." The authors are segment 0
        # (they contain commas / "and"); the title is the next segment.
        segs = [s.strip() for s in clean.split('.') if len(s.strip()) > 0]
        cand = ""
        if segs:
            if len(segs) >= 2 and ("," in segs[0] or " and " in segs[0].lower()):
                cand = segs[1]
            else:
                cand = segs[0]
        # Reject candidates that are just numbers/years or too short/long.
        if cand and 8 < len(cand) < 250 and not re.fullmatch(r'[\d,\s]+', cand):
            info["title"] = cand

    # Fallback for numbered/IEEE comma-format entries where authors are
    # "F. A. Surname," and the title is a comma-delimited segment before the
    # venue (the period-split heuristic fails on the initials' periods).
    if not info["title"]:
        t = re.sub(r'^\s*\[\d+\]\s*', '', clean)
        prev = None
        while t != prev:
            prev = t
            t = re.sub(r'^(?:[A-Z]\.[- ]?){1,3}\s*[A-Z][\w\u2019\'-]+\s*,\s*', '', t)   # "Y. A. Malkov,"
            t = re.sub(r'^[A-Z][\w\u2019\'-]+\s*,\s*(?:[A-Z]\.[- ]?){1,3}\s*,?\s*', '', t)  # "Malkov, Y. A.,"
            t = re.sub(r'^and\s+', '', t)
        cand = re.split(
            r',\s*(?:IEEE|ACM|Proc\b|Proceedings|Journal|Trans\b|Advances|arXiv|CoRR|Nature|Science|In\s|vol\b|Vol\b|pp\b)',
            t, maxsplit=1,
        )[0].strip().rstrip('.')
        if cand and 10 < len(cand) < 250 and not re.fullmatch(r'[\d,\s]+', cand) and re.search(r'[a-z]{3}', cand):
            info["title"] = cand

    return info


# ---------------------------------------------------------------------------
# Author-year (unnumbered) bibliography support.
# Many real papers (ACL/NeurIPS/ICML style) have NO [n] markers: the body cites
# "(Vaswani et al., 2017)" and the bibliography lists "Vaswani, A., ... 2017."
# or "Ashish Vaswani, Noam Shazeer, ... 2017. Attention is all you need. ...".
# These entries are keyed "surnameYEAR" (e.g. "vaswani2017") so claim-side
# author-year citations map onto them.
# ---------------------------------------------------------------------------

# An entry START is a line beginning with an author list, either
# surname-first ("Abadi, M.,") or firstname-first ("Alan Akbik,").
_AY_CHARS = r"A-Za-z\u00c0-\u017f\u00a8\u00b4'\u2019\-"

# Lowercase nobiliary particles that can precede a surname ("van der Aalst").
_AY_NOBILIARY = r"(?:van|von|de|del|della|der|den|di|da|dos|das|du|la|le|el|ter|ten|te|st)"

_AY_ENTRY_START = re.compile(
    r"(?:^|\n)\s*(?:"
    r"(?:" + _AY_NOBILIARY + r"\s+){0,3}[A-Z][" + _AY_CHARS + r"]{1,30}(?:\s+[A-Z][" + _AY_CHARS + r"]{1,30}){0,2},\s+[A-Z][a-z]?\.?"   # (van der) Surname, F.
    r"|[A-Z][a-z]{1,30}\s+(?:[A-Z]\.?\s+)?(?:" + _AY_NOBILIARY + r"\s+){0,3}[A-Z][" + _AY_CHARS + r"]{1,30}(?:,\s|\s+and\s+)"  # First (van) Last(, / and)
    r"|(?:[A-Z]\.-?)+\s+(?:" + _AY_NOBILIARY + r"\s+){0,3}[A-Z][" + _AY_CHARS + r"]{1,30},\s"  # J. (van) Deng,
    r")"
)
_AY_YEAR = re.compile(r"(?<![\d.])(?:19|20)\d{2}[a-z]?(?![\d])")
_AY_STRIP_IDS = re.compile(
    r"arXiv[:\s]*\d{4}\.\d{4,5}(v\d+)?|abs/\d{4}\.\d{4,5}|10\.\d{4,}/\S+|https?://\S+",
    re.IGNORECASE,
)



def _surname_head(phrase: str) -> str:
    """The key surname from a name phrase: the FIRST capitalized word, matching
    the claim side (which keys "(van der Aalst, 1999)" -> "aalst"). Particles
    are lowercase and thus skipped automatically."""
    caps = re.findall(r"[A-Z][" + _AY_CHARS + r"]*", phrase)
    return caps[0] if caps else ""


def _all_author_surnames(head: str) -> list:
    """Every author surname in an author-list head (particle-aware), so a
    citation by a co-author -- e.g. "(van der Aalst, 2008)" for an entry whose
    FIRST author is someone else -- still resolves."""
    C = _AY_CHARS
    out = []
    for m in re.finditer(
        r"((?:" + _AY_NOBILIARY + r"\s+){0,3}[A-Z][" + C + r"]{1,30}(?:\s+[A-Z][" + C + r"]{1,30}){0,2}),\s*(?:[A-ZÀ-Þ]\.\s*-?){1,4}",
        head,
    ):
        # A surname phrase may be compound ("Atsa Etoundi", "Fouda Ndjodo"); the
        # paper may cite by ANY of its capitalized words, so register them all.
        for w in re.findall(r"[A-Z][" + C + r"]*", m.group(1)):
            if len(w) >= 2:
                out.append(w)
    return out


def _first_author_surname(entry_text: str) -> str:
    """First author's surname from the start of a bibliography entry, keyed the
    same way the claim side keys inline citations (particle-aware)."""
    head = entry_text.lstrip()[:200]
    C = _AY_CHARS
    # surname-first, tolerating leading lowercase particles: "van der Aalst, W.M.P."
    m = re.match(
        r"((?:" + _AY_NOBILIARY + r"\s+){0,3}[A-Z][" + C + r"]{1,30}(?:\s+[A-Z][" + C + r"]{1,30}){0,2}),\s+[A-Z\u00c0-\u00de]\.",
        head,
    )
    if m:  # surname-first (post-comma is an initial like "W." / "R.")
        return _surname_head(m.group(1))
    m = re.match(r"[A-Z][a-z]{1,30}\s+(?:[A-Z]\.?\s+)?((?:" + _AY_NOBILIARY + r"\s+){0,3}[A-Z][" + C + r"]{1,30}),", head)
    if m:  # firstname-first "Alan Akbik," / "Wil van der Aalst,"
        # surname is the LAST capitalized word here (first name precedes it)
        caps = re.findall(r"[A-Z][" + C + r"]*", m.group(1))
        return caps[0] if caps else ""
    m = re.match(r"(?:[A-Z]\.-?)+\s+((?:" + _AY_NOBILIARY + r"\s+){0,3}[A-Z][" + C + r"]{1,30}),", head)
    if m:  # initial-first: "J. Deng," / "W. van der Aalst,"
        return _surname_head(m.group(1))
    m = re.match(r"[A-Z][a-z]{1,30}\s+(?:[A-Z]\.\s+)?([A-Z][" + C + r"]{1,30})\s+and\s+", head)
    if m:  # firstname-first, two-author "Jeremy Howard and Sebastian Ruder."
        return m.group(1)
    return ""


def _looks_like_bib_entry(text: str) -> bool:
    """A real bibliography entry almost always carries a year or an identifier."""
    return bool(_AY_YEAR.search(_AY_STRIP_IDS.sub(" ", text))
                or re.search(r"arxiv|doi|https?://", text, re.IGNORECASE))


# Venue / container markers that terminate a title.
_AY_VENUE = re.compile(
    r"\.?\s+(?:In\s|arXiv\b|CoRR\b|Journal\b|Proceedings\b|Advances\b|Nature\b|"
    r"Science\b|Cell\b|IEEE\b|ACM\b|Transactions\b|pp\.|pages\s|"
    r"Techn(?:ical|ology)\b|Neural Information\b|Association for\b|volume\s|vol\.)",
    re.IGNORECASE,
)


# Leading author-list stripping for title extraction. The style is decided ONCE
# from the first unit (the two styles are otherwise ambiguous and an alternation
# misparses). Surname-first: "Abadi, M." / "Alayrac, J.-B." / "De Fauw, J.".
# Firstname-first: "Alan Akbik" / "Rami Al-Rfou". Multi-word surnames allowed.
_AY_SURNAME = r"[A-Z][" + _AY_CHARS + r"]{0,29}(?:\s+[A-Z][a-z][" + _AY_CHARS + r"]{0,29})?"
_AY_INITIALS = r"(?:[A-Z]\u2019?\.-?\s?)*[A-Z]\u2019?\."   # must END with a period
_AY_PARTICLE = r"(?:\s+[a-z]{1,3}\.)?"                        # "Freitas, N. d."
_AY_FF_FIRST = r"[A-Z][" + _AY_CHARS + r"]{1,30}"               # "Minh-Thang" ok
_AY_SF_PROBE = re.compile(r"^\s*" + _AY_SURNAME + r",\s+" + _AY_INITIALS)
_AY_FF_PROBE = re.compile(r"^\s*" + _AY_FF_FIRST + r"\s+(?:[A-Z]\.?\s+)?" + _AY_SURNAME + r"[,\.\s]|^\s*" + _AY_FF_FIRST + r"\s+" + _AY_SURNAME + r"\s+and\s+")
_AY_SF_UNIT = re.compile(r"^\s*" + _AY_SURNAME + r",\s+" + _AY_INITIALS + _AY_PARTICLE + r",?\s*")
_AY_FF_UNIT = re.compile(r"^\s*" + _AY_FF_FIRST + r"\s+(?:[A-Z]\.?\s+)?" + _AY_SURNAME + r"[.,]?\s*")
_AY_IF_PROBE = re.compile(r"^\s*(?:[A-Z]\.-?)+\s+" + _AY_SURNAME)
_AY_IF_UNIT = re.compile(r"^\s*(?:[A-Z]\.-?)+\s+" + _AY_SURNAME + r"[.,]?\s*")
_AY_CONNECTOR = re.compile(r"^\s*(?:and|&)\s+")
_AY_ETAL = re.compile(r"^,?\s*et\s+al\.?\s*", re.IGNORECASE)


def _ay_strip_authors(text: str) -> str:
    """Remove the leading author list; stop at the first non-author text."""
    if _AY_SF_PROBE.match(text):
        unit = _AY_SF_UNIT
    elif _AY_IF_PROBE.match(text):
        unit = _AY_IF_UNIT
    elif _AY_FF_PROBE.match(text):
        unit = _AY_FF_UNIT
    else:
        return text
    rest = text
    for _ in range(60):  # hard cap; author lists are finite
        for pat in (_AY_ETAL, _AY_CONNECTOR, unit):
            new = pat.sub("", rest, count=1)
            if new != rest:
                rest = new
                break
        else:
            break
    return rest


def _ay_extract_title(entry: str) -> str:
    """Best-effort title for an author-year bibliography entry."""
    clean = _AY_STRIP_IDS.sub(" ", entry)
    clean = re.sub(r"\s+", " ", clean).strip()
    quoted = re.search(r'["\u201c\u201d\u2018\u2019](.+?)["\u201c\u201d\u2018\u2019]', clean)
    if quoted and len(quoted.group(1)) > 6:
        return quoted.group(1).strip()
    rest = _ay_strip_authors(clean).lstrip(". \u2019'")
    # ACL style: "2018. Title. In Venue..." — skip a leading year segment.
    m = re.match(r"^(?:19|20)\d{2}[a-z]?\.\s*", rest)
    if m:
        rest = rest[m.end():]
    # Title runs until the venue/container marker.
    vm = _AY_VENUE.search(rest)
    title = rest[:vm.start()] if vm else rest
    title = title.strip().strip(".").strip()
    # Drop a trailing ", 2016"-type fragment.
    title = re.sub(r",?\s*(?:19|20)\d{2}[a-z]?$", "", title).strip().strip(",")
    if 8 < len(title) < 250:
        return title
    return ""


def _parse_author_year_refs(ref_section: str) -> dict:
    """Parse an unnumbered (author-year) bibliography into surnameYEAR-keyed refs."""
    starts = []
    for m in _AY_ENTRY_START.finditer(ref_section):
        s = m.start()
        # A wrapped author-list continuation line ("Bernardo Magnini,") also
        # matches the start pattern; only accept a start when the preceding
        # entry actually ended (sentence-final period) or at section start.
        prev = ref_section[:s].rstrip()
        if prev and not prev.endswith((".", "}", "\u201d", '"')):
            continue
        starts.append(s)
    refs = {}
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(ref_section)
        entry = ref_section[s:e].strip()
        # PDF line wraps hyphen-split words ("Robin-\nson", "Man-\nning").
        entry = re.sub(r"([A-Za-z])-\s*\n\s*([a-z])", r"\1\2", entry)
        if len(entry) < 25:
            continue
        surname = _first_author_surname(entry)
        if not surname:
            continue
        ym = _AY_YEAR.search(_AY_STRIP_IDS.sub(" ", entry))
        if not ym:
            continue
        key = f"{surname.lower()}{ym.group(0)}"
        # Collision: same surname + year (2018a/2018b handled via the year
        # suffix when present; otherwise disambiguate numerically).
        if key in refs:
            n = 2
            while f"{key}~{n}" in refs:
                n += 1
            key = f"{key}~{n}"
        info = _parse_single_reference(entry)
        info["year"] = ym.group(0)[:4] if len(ym.group(0)) == 4 else ym.group(0)
        info["authors"] = surname
        title = _ay_extract_title(entry)
        if title:
            info["title"] = title
        refs[key] = info
        # Also index by every author surname + year, so a citation by a
        # co-author resolves to this same entry (the first-author key wins).
        yr = ym.group(0)
        head_by = entry[:entry.find(yr)] if yr in entry else entry[:150]
        for sn in _all_author_surnames(head_by):
            k2 = f"{sn.lower()}{yr}"
            if k2 not in refs:
                refs[k2] = info
    return refs


def fetch_source_content(ref_info: dict, max_chars: int = 15000) -> Optional[str]:
    """
    Given parsed reference info, attempt to fetch the actual source content.
    Tries in order: arXiv → DOI → URL → PubMed → title search.
    Returns the text content or None if inaccessible.
    """
    content = None

    # 1. Try arXiv (best source — full paper text)
    if ref_info.get("arxiv_id"):
        content = _fetch_arxiv(ref_info["arxiv_id"])
        if content:
            return content[:max_chars]

    # 2. Try DOI (resolve to actual URL, then fetch)
    if ref_info.get("doi"):
        content = _fetch_doi(ref_info["doi"])
        if content:
            return content[:max_chars]

    # 3. Try direct URL
    if ref_info.get("url"):
        content = _fetch_url_safe(ref_info["url"])
        if content:
            return content[:max_chars]

    # 4. Try PubMed (abstract at minimum)
    if ref_info.get("pmid"):
        content = _fetch_pubmed_abstract(ref_info["pmid"])
        if content:
            return content[:max_chars]

    # 5. Fallback: search by title
    if ref_info.get("title"):
        content = _search_and_fetch(ref_info["title"])
        if content:
            return content[:max_chars]

    # 6. Last resort: search by raw text
    if ref_info.get("raw"):
        # Extract a meaningful search query from the raw reference
        query = re.sub(r'[^\w\s]', ' ', ref_info["raw"])[:100]
        content = _search_and_fetch(query)
        if content:
            return content[:max_chars]

    return None


def _fetch_arxiv(arxiv_id: str) -> Optional[str]:
    """Fetch full text of an arXiv paper by downloading its PDF."""
    try:
        import tempfile
        # Download the actual PDF (open access)
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        resp = _get_with_backoff(
            pdf_url,
            timeout=30,
            headers={"User-Agent": "HallucinationNerd/1.0"},
        )
        if resp is not None and resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/pdf"):
            # Save to temp file and extract text
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(resp.content)
                tmp_path = tmp.name
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(tmp_path)
                text = ""
                for page in doc:
                    text += page.get_text()
                doc.close()
                if len(text) > 100:
                    return text
            finally:
                import os
                os.unlink(tmp_path)

        # Fallback: get abstract from HTML page
        abs_url = f"https://arxiv.org/abs/{arxiv_id}"
        resp = _get_with_backoff(abs_url, timeout=15, headers={"User-Agent": "HallucinationNerd/1.0"})
        if resp is not None and resp.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            abstract_block = soup.find("blockquote", class_="abstract")
            if abstract_block:
                abstract = abstract_block.get_text(strip=True).replace("Abstract:", "").strip()
                title_el = soup.find("h1", class_="title")
                title = title_el.get_text(strip=True).replace("Title:", "").strip() if title_el else ""
                return f"Title: {title}\n\nAbstract: {abstract}"
    except Exception:
        pass
    return None


def _fetch_doi(doi: str) -> Optional[str]:
    """Resolve a DOI — try Unpaywall for free PDF first, then landing page."""
    # Try Unpaywall first (finds free/open-access versions)
    try:
        unpaywall_url = f"https://api.unpaywall.org/v2/{doi}?email={os.getenv('UNPAYWALL_EMAIL', 'hallucinationnerd@example.com')}"
        resp = _get_with_backoff(unpaywall_url, timeout=10)
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            # Look for a free PDF URL
            best_oa = data.get("best_oa_location", {})
            if best_oa:
                pdf_url = best_oa.get("url_for_pdf") or best_oa.get("url")
                if pdf_url and "arxiv.org" in pdf_url:
                    # It's an arXiv link — extract ID and use our arXiv fetcher
                    import re
                    arxiv_match = re.search(r'(\d{4}\.\d{4,5})', pdf_url)
                    if arxiv_match:
                        return _fetch_arxiv(arxiv_match.group(1))
                elif pdf_url and pdf_url.endswith('.pdf'):
                    # Direct PDF link — download and extract
                    return _fetch_pdf_from_url(pdf_url)
                elif pdf_url:
                    # HTML page — crawl it
                    return _fetch_url_safe(pdf_url)
    except Exception:
        pass

    # Fallback: resolve DOI to landing page and scrape
    try:
        url = f"https://doi.org/{doi}"
        resp = _get_with_backoff(url, timeout=15, allow_redirects=True,
                          headers={"User-Agent": "HallucinationNerd/1.0", "Accept": "text/html"})
        if resp is not None and resp.status_code == 200 and len(resp.text) > 500:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            if len(text) > 200:
                return text
    except Exception:
        pass
    return None


def _fetch_pdf_from_url(url: str) -> Optional[str]:
    """Download a PDF from a URL and extract text."""
    try:
        import tempfile
        resp = _get_with_backoff(url, timeout=30, headers={"User-Agent": "HallucinationNerd/1.0"})
        if resp is not None and resp.status_code == 200 and len(resp.content) > 1000:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(resp.content)
                tmp_path = tmp.name
            try:
                import fitz
                doc = fitz.open(tmp_path)
                text = ""
                for page in doc:
                    text += page.get_text()
                doc.close()
                if len(text) > 100:
                    return text
            finally:
                import os
                os.unlink(tmp_path)
    except Exception:
        pass
    return None


def _fetch_url_safe(url: str) -> Optional[str]:
    """Fetch a URL and extract text content."""
    try:
        resp = _get_with_backoff(url, timeout=15, allow_redirects=True,
                          headers={"User-Agent": "HallucinationNerd/1.0"})
        if resp is not None and resp.status_code == 200 and len(resp.text) > 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            if len(text) > 100:
                return text
    except Exception:
        pass
    return None


def _fetch_pubmed_abstract(pmid: str) -> Optional[str]:
    """Fetch a PubMed abstract by PMID."""
    try:
        url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={pmid}&rettype=abstract&retmode=text"
        resp = _get_with_backoff(url, timeout=15)
        if resp is not None and resp.status_code == 200 and len(resp.text) > 50:
            return resp.text
    except Exception:
        pass
    return None


def _search_and_fetch(query: str) -> Optional[str]:
    """Search for a paper by title/query and return its full text. Tries arXiv search first (no rate limit), then Semantic Scholar, then PubMed."""
    # Try arXiv search first (free, no rate limit, covers most CS/ML papers)
    result = _search_arxiv_by_title(query)
    if result:
        return result

    # Try OpenAlex (separate infra, survives arXiv/S2 outages, and its arXiv
    # locations fetch fine even when the export API 429s)
    result = _search_openalex(query)
    if result:
        return result

    # Try Semantic Scholar (rate limited without API key)
    result = _search_semantic_scholar(query)
    if result:
        return result

    # Fall back to PubMed (biomedical)
    try:
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        params = {"db": "pubmed", "term": query, "retmax": 1, "retmode": "json"}
        resp = _get_with_backoff(search_url, params=params, timeout=10)
        if resp is None or resp.status_code != 200:
            return None

        data = resp.json()
        ids = data.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return None

        # Relevance guard, same standard as the arXiv path: PubMed's top hit
        # for a non-biomedical reference is usually an unrelated paper, and
        # without a title check an arXiv outage silently "resolves" refs to
        # wrong papers (observed: an ELMo citation resolved to a 2026
        # Transformer-GNN news-transcription article).
        cand_title = _pubmed_title(ids[0])
        if cand_title and not _title_matches(query, cand_title):
            return None

        return _fetch_pubmed_abstract(ids[0])
    except Exception:
        return None


def _pubmed_title(pmid: str) -> str:
    """Fetch a PubMed article's title via esummary, for the relevance guard.

    Returns "" when the lookup fails; callers fail open in that case so a
    transient esummary hiccup does not cost a legitimately matched abstract.
    """
    try:
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        resp = _get_with_backoff(url, params={"db": "pubmed", "id": pmid, "retmode": "json"}, timeout=10)
        if resp is not None and resp.status_code == 200:
            doc = resp.json().get("result", {}).get(str(pmid), {}) or {}
            return doc.get("title", "") or ""
    except Exception:
        pass
    return ""


def _title_matches(query: str, title: str, min_overlap: float = 0.5) -> bool:
    """True only if the candidate title genuinely matches the reference title.

    Guards against arXiv's top-hit returning an unrelated paper for a reference
    we can't precisely resolve (e.g. matching an enterprise-control citation to
    'Cosmopolitan Sexualities'). Requires that at least `min_overlap` of the
    reference's significant words appear in the candidate title.
    """
    def toks(s):
        return {w for w in re.sub(r'[^\w\s]', ' ', (s or '').lower()).split() if len(w) > 3}
    q, t = toks(query), toks(title)
    if not q:
        return False
    return (len(q & t) / len(q)) >= min_overlap


def _search_arxiv_by_title(query: str) -> Optional[str]:
    """Search arXiv API by title and download the full PDF if the hit actually
    matches the reference title. Rate-limited like every other external call;
    the API 429s/times out under parallel load otherwise."""
    try:
        import urllib.parse
        # arXiv API search (https avoids the http->https redirect round-trip)
        clean_query = re.sub(r'[^\w\s]', ' ', query).strip()
        search_url = f"https://export.arxiv.org/api/query?search_query=ti:{urllib.parse.quote(clean_query[:100])}&max_results=1"
        resp = _get_with_backoff(search_url, timeout=20)
        if resp is None or resp.status_code != 200:
            return None

        # Parse the Atom XML response
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        entries = root.findall('atom:entry', ns)
        if not entries:
            return None

        entry = entries[0]

        # Relevance guard: only accept the hit if its title matches the reference.
        title_el = entry.find('atom:title', ns)
        cand_title = title_el.text.strip() if title_el is not None else ""
        if not _title_matches(query, cand_title):
            return None

        # Get the arXiv ID from the entry
        entry_id = entry.find('atom:id', ns)
        if entry_id is None:
            return None

        # Extract arXiv ID from URL like http://arxiv.org/abs/2301.12345v1
        arxiv_id_match = re.search(r'(\d{4}\.\d{4,5})', entry_id.text)
        if arxiv_id_match:
            arxiv_id = arxiv_id_match.group(1)
            # Download full PDF
            full_text = _fetch_arxiv(arxiv_id)
            if full_text and len(full_text) > 500:
                return full_text

        # Fallback: return title + abstract from the API response
        summary_el = entry.find('atom:summary', ns)
        abstract = summary_el.text.strip() if summary_el is not None else ""
        if abstract:
            return f"Title: {cand_title}\n\nAbstract: {abstract}"
    except Exception:
        pass
    return None


def _search_openalex(query: str) -> Optional[str]:
    """Search OpenAlex by title and follow the arXiv location when it matches.

    OpenAlex indexes arXiv preprints alongside venues, runs on separate
    infrastructure from the arXiv export API and Semantic Scholar, and stays
    responsive when both rate-limit us. Same relevance standard as the other
    legs: the title guard applies before any fetch.
    """
    try:
        resp = _get_with_backoff(
            "https://api.openalex.org/works",
            params={"search": query[:120], "per-page": 5},
            headers={"User-Agent": "HallucinationNerd/1.0 (citation verification)"},
            timeout=12,
        )
        if resp is None or resp.status_code != 200:
            return None
        for work in resp.json().get("results", []) or []:
            cand_title = str(work.get("display_name", "") or "")
            if not cand_title or not _title_matches(query, cand_title):
                continue
            for loc in work.get("locations", []) or []:
                for u in (loc.get("landing_page_url"), loc.get("pdf_url")):
                    u = str(u or "")
                    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(v\d+)?", u)
                    if m:
                        text = _fetch_arxiv(m.group(1))
                        if text:
                            return text
            # Title matched but no usable arXiv location: keep looking.
        return None
    except Exception:
        return None


def _search_semantic_scholar(query: str) -> Optional[str]:
    """Search Semantic Scholar API for a paper and return its full text if on arXiv, otherwise title + abstract."""
    try:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {"query": query[:200], "limit": 1, "fields": "title,abstract,externalIds"}
        headers = {"User-Agent": "HallucinationNerd/1.0"}
        if _S2_API_KEY:
            headers["x-api-key"] = _S2_API_KEY
        resp = _get_with_backoff(url, params=params, timeout=15, headers=headers)
        if resp is None or resp.status_code != 200:
            return None

        data = resp.json()
        papers = data.get("data", [])
        if not papers:
            return None

        paper = papers[0]
        title = paper.get("title", "")
        abstract = paper.get("abstract", "")

        # Relevance guard, same standard as the arXiv path: reject a top hit
        # whose title does not match the reference. Without this, S2's fuzzy
        # search silently "resolves" refs to unrelated papers.
        if title and not _title_matches(query, title):
            return None

        # If paper has an arXiv ID, fetch the full PDF instead of just abstract
        external_ids = paper.get("externalIds", {}) or {}
        arxiv_id = external_ids.get("ArXiv", "")
        if arxiv_id:
            full_text = _fetch_arxiv(arxiv_id)
            if full_text and len(full_text) > 500:
                return full_text

        # Fallback to abstract
        if abstract:
            return f"Title: {title}\n\nAbstract: {abstract}"
        elif title:
            return f"Title: {title}"
    except Exception:
        pass
    return None


def _make_cache_key(ref_info: dict, ref_key: str) -> str:
    """Stable cache key for a parsed reference.

    The previous key (first 100 chars of `raw` text) caused collisions
    whenever two references had the same opening — common for repeated
    author names, long titles getting truncated the same way, etc.

    New key: prefer canonical identifiers (arxiv_id > doi > pmid > title).
    Falls back to a hash of (title + arxiv_id + doi) for the long tail.
    """
    arxiv = (ref_info.get("arxiv_id") or "").strip().lower()
    if arxiv:
        return f"arxiv:{arxiv}"
    doi = (ref_info.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    pmid = (ref_info.get("pmid") or "").strip()
    if pmid:
        return f"pmid:{pmid}"
    title = (ref_info.get("title") or ref_info.get("raw") or ref_key).strip().lower()
    if title:
        import hashlib
        return "title:" + hashlib.sha256(title.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"raw:{ref_key}"


def _search_pubmed(query: str) -> Optional[str]:
    """Search PubMed by query and return the top hit's abstract (backup search)."""
    try:
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        params = {"db": "pubmed", "term": query, "retmax": 1, "retmode": "json"}
        resp = _get_with_backoff(search_url, params=params, timeout=10)
        if resp is None or resp.status_code != 200:
            return None
        ids = resp.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return None
        return _fetch_pubmed_abstract(ids[0])
    except Exception:
        return None


# User-selectable backup-search databases (source-database checkboxes in the web UI).
_DB_SEARCHERS = {
    "pubmed": ("PubMed", _search_pubmed),
    "arxiv": ("arXiv", _search_arxiv_by_title),
    "semantic_scholar": ("Semantic Scholar", _search_semantic_scholar),
}

DEFAULT_BACKUP_DATABASES = ["pubmed", "arxiv", "semantic_scholar"]


def backup_search(query: str, databases: list, custom_url_template: str = ""):
    """
    Backup reference search for a claim that carries NO inline citation.

    Searches only the user-selected `databases` (keys from _DB_SEARCHERS), in the
    order given, plus an optional user-supplied database via a search-URL template
    containing the literal '{query}'. Backs the source-database checkboxes in the
    web UI. Returns (content, source_label) for the first database that yields
    usable content, else (None, None).
    """
    for db in databases:
        key = str(db).strip().lower().replace(" ", "_").replace("-", "_")
        entry = _DB_SEARCHERS.get(key)
        if not entry:
            continue
        label, fn = entry
        try:
            content = fn(query)
        except Exception:
            content = None
        if content and len(content) > 100:
            return content, label

    if custom_url_template and "{query}" in custom_url_template:
        import urllib.parse
        url = custom_url_template.replace("{query}", urllib.parse.quote(query[:200]))
        content = _fetch_url_safe(url)
        if content and len(content) > 100:
            return content, "Custom database"

    return None, None


class ResolutionResult(dict):
    """Resolved-source mapping with refs still throttled after retries."""

    def __init__(self, *args, throttled_refs=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.throttled_refs = throttled_refs or []


def _fetch_with_policy(ref_info: dict, patient: bool):
    _request_policy.attempts = _PATIENT_REQUEST_ATTEMPTS if patient else _DEFAULT_REQUEST_ATTEMPTS
    _request_policy.throttled = False
    content = fetch_source_content(ref_info)
    return content, (not content and bool(getattr(_request_policy, "throttled", False)))


def resolve_and_fetch_all(full_text: str, cited_refs: list, patient: bool = False) -> dict:
    """
    Main entry point: given full document text and a list of citation markers
    (e.g., ["1", "2"]), resolve each to actual content.
    Uses parallel fetching for speed (5 concurrent downloads).

    Returns: {"1": "content text...", "2": None, ...}
    """
    # Parse references section
    refs = resolve_references(full_text)

    # Check cache first, build list of refs that need fetching
    results = {}
    to_fetch = []
    ttl = int(os.getenv("HVE_CACHE_TTL", str(_CACHE_TTL_SECONDS)))
    now = _time.time()
    # Garbage-collect expired entries
    for k in [k for k, v in _source_cache.items() if now - v[0] > ttl]:
        _source_cache.pop(k, None)

    for ref_key in cited_refs:
        ref_key_str = str(ref_key)
        ref_info = refs.get(ref_key_str, {})
        cache_key = _make_cache_key(ref_info, ref_key_str)
        cached = _source_cache.get(cache_key)
        if cached is not None:
            _, content = cached
            results[ref_key_str] = content
            continue
        ref_info = refs.get(ref_key_str)
        if ref_info is None and not ref_key_str.isdigit():
            # Author-year key miss: tolerate year-suffix / disambiguation drift
            # ("vaswani2017" vs stored "vaswani2017~2") by surname+year prefix.
            m = re.match(r"^([a-z\u00c0-\u017f'\-]+)((?:19|20)\d{2})", ref_key_str)
            if m:
                prefix = m.group(1) + m.group(2)
                for k in (prefix, prefix + "a", prefix + "b", prefix + "~2"):
                    if k in refs:
                        ref_info = refs[k]
                        cache_key = _make_cache_key(ref_info, k)
                        break
        if ref_info is not None:
            to_fetch.append((ref_key_str, ref_info, cache_key))
        else:
            results[ref_key_str] = None

    # Parallel fetch (5 workers — fast but polite)
    throttled_refs = []
    if to_fetch:
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_ref = {}
            for ref_key_str, ref_info, cache_key in to_fetch:
                future = executor.submit(_fetch_with_policy, ref_info, patient)
                future_to_ref[future] = (ref_key_str, cache_key)

            for future in as_completed(future_to_ref):
                ref_key_str, cache_key = future_to_ref[future]
                try:
                    content, throttled = future.result()
                    results[ref_key_str] = content
                    if throttled:
                        throttled_refs.append(ref_key_str)
                    # Cache ONLY successful fetches. Caching None poisons the
                    # cache: a single transient failure (e.g. arXiv rate-limit)
                    # would otherwise stick for the whole TTL and never retry.
                    if content:
                        _source_cache[cache_key] = (now, content)
                except Exception:
                    results[ref_key_str] = None

    return ResolutionResult(results, throttled_refs=throttled_refs)
