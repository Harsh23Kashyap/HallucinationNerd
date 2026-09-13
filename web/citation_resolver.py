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
            return text[match.end():]

    # Fallback: look for the last section that starts with [1] or 1.
    match = re.search(r'\n\s*\[1\]\s+\w', text)
    if match:
        return text[match.start():]

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

_AY_ENTRY_START = re.compile(
    r"(?:^|\n)\s*(?:"
    r"[A-Z][" + _AY_CHARS + r"]{1,30},\s+[A-Z][a-z]?\.?"   # Surname, F.
    r"|[A-Z][a-z]{1,30}\s+(?:[A-Z]\.\s+)?[A-Z][" + _AY_CHARS + r"]{1,30},\s"  # First Last,
    r")"
)
_AY_YEAR = re.compile(r"(?<![\d.])(?:19|20)\d{2}[a-z]?(?![\d])")
_AY_STRIP_IDS = re.compile(
    r"arXiv[:\s]*\d{4}\.\d{4,5}(v\d+)?|abs/\d{4}\.\d{4,5}|10\.\d{4,}/\S+|https?://\S+",
    re.IGNORECASE,
)


def _first_author_surname(entry_text: str) -> str:
    """First author's surname from the start of a bibliography entry."""
    head = entry_text.lstrip()[:200]
    m = re.match(r"([A-Z][" + _AY_CHARS + r"]{1,30}),\s+[A-Z][a-z]?\.?", head)
    if m:  # surname-first
        return m.group(1)
    m = re.match(r"[A-Z][a-z]{1,30}\s+(?:[A-Z]\.\s+)?([A-Z][" + _AY_CHARS + r"]{1,30}),", head)
    if m:  # firstname-first
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
_AY_FF_PROBE = re.compile(r"^\s*" + _AY_FF_FIRST + r"\s+(?:[A-Z]\.\s+)?" + _AY_SURNAME + r"[,\.\s]")
_AY_SF_UNIT = re.compile(r"^\s*" + _AY_SURNAME + r",\s+" + _AY_INITIALS + _AY_PARTICLE + r",?\s*")
_AY_FF_UNIT = re.compile(r"^\s*" + _AY_FF_FIRST + r"\s+(?:[A-Z]\.\s+)?" + _AY_SURNAME + r"[.,]?\s*")
_AY_CONNECTOR = re.compile(r"^\s*(?:and|&)\s+")
_AY_ETAL = re.compile(r"^,?\s*et\s+al\.?\s*", re.IGNORECASE)


def _ay_strip_authors(text: str) -> str:
    """Remove the leading author list; stop at the first non-author text."""
    if _AY_SF_PROBE.match(text):
        unit = _AY_SF_UNIT
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
    _rate_limit()
    try:
        import tempfile
        # Download the actual PDF (open access)
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        # Retry with backoff: arXiv rate-limits (429) and occasionally 5xxs;
        # a single miss should not silently drop the source.
        resp = None
        for _attempt in range(2):
            try:
                resp = requests.get(pdf_url, timeout=30, headers={"User-Agent": "HallucinationNerd/1.0"})
            except requests.RequestException:
                resp = None
            if resp is not None and resp.status_code == 200:
                break
            if resp is not None and resp.status_code not in (429, 500, 502, 503, 504):
                break
            _time.sleep(0.6 * (_attempt + 1))
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
        resp = requests.get(abs_url, timeout=15, headers={"User-Agent": "HallucinationNerd/1.0"})
        if resp.status_code == 200:
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
    _rate_limit()
    try:
        unpaywall_url = f"https://api.unpaywall.org/v2/{doi}?email={os.getenv('UNPAYWALL_EMAIL', 'hallucinationnerd@example.com')}"
        resp = requests.get(unpaywall_url, timeout=10)
        if resp.status_code == 200:
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
    _rate_limit()
    try:
        url = f"https://doi.org/{doi}"
        resp = requests.get(url, timeout=15, allow_redirects=True,
                          headers={"User-Agent": "HallucinationNerd/1.0", "Accept": "text/html"})
        if resp.status_code == 200 and len(resp.text) > 500:
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
    _rate_limit()
    try:
        import tempfile
        resp = requests.get(url, timeout=30, headers={"User-Agent": "HallucinationNerd/1.0"})
        if resp.status_code == 200 and len(resp.content) > 1000:
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
    _rate_limit()
    try:
        resp = requests.get(url, timeout=15, allow_redirects=True,
                          headers={"User-Agent": "HallucinationNerd/1.0"})
        if resp.status_code == 200 and len(resp.text) > 200:
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
    _rate_limit()
    try:
        url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={pmid}&rettype=abstract&retmode=text"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200 and len(resp.text) > 50:
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

    # Try Semantic Scholar (rate limited without API key)
    result = _search_semantic_scholar(query)
    if result:
        return result

    # Fall back to PubMed (biomedical)
    _rate_limit()
    try:
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        params = {"db": "pubmed", "term": query, "retmax": 1, "retmode": "json"}
        resp = requests.get(search_url, params=params, timeout=10)
        if resp.status_code != 200:
            return None

        data = resp.json()
        ids = data.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return None

        return _fetch_pubmed_abstract(ids[0])
    except Exception:
        return None


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
    matches the reference title. No rate limit."""
    try:
        import urllib.parse
        # arXiv API search
        clean_query = re.sub(r'[^\w\s]', ' ', query).strip()
        search_url = f"http://export.arxiv.org/api/query?search_query=ti:{urllib.parse.quote(clean_query[:100])}&max_results=1"
        resp = requests.get(search_url, timeout=15)
        if resp.status_code != 200:
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


def _search_semantic_scholar(query: str) -> Optional[str]:
    """Search Semantic Scholar API for a paper and return its full text if on arXiv, otherwise title + abstract."""
    _rate_limit()
    try:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {"query": query[:200], "limit": 1, "fields": "title,abstract,externalIds"}
        headers = {"User-Agent": "HallucinationNerd/1.0"}
        if _S2_API_KEY:
            headers["x-api-key"] = _S2_API_KEY
        resp = requests.get(url, params=params, timeout=15, headers=headers)
        if resp.status_code != 200:
            return None

        data = resp.json()
        papers = data.get("data", [])
        if not papers:
            return None

        paper = papers[0]
        title = paper.get("title", "")
        abstract = paper.get("abstract", "")
        
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
    _rate_limit()
    try:
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        params = {"db": "pubmed", "term": query, "retmax": 1, "retmode": "json"}
        resp = requests.get(search_url, params=params, timeout=10)
        if resp.status_code != 200:
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


def resolve_and_fetch_all(full_text: str, cited_refs: list) -> dict:
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
    if to_fetch:
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_ref = {}
            for ref_key_str, ref_info, cache_key in to_fetch:
                future = executor.submit(fetch_source_content, ref_info)
                future_to_ref[future] = (ref_key_str, cache_key)

            for future in as_completed(future_to_ref):
                ref_key_str, cache_key = future_to_ref[future]
                try:
                    content = future.result()
                    results[ref_key_str] = content
                    # Cache ONLY successful fetches. Caching None poisons the
                    # cache: a single transient failure (e.g. arXiv rate-limit)
                    # would otherwise stick for the whole TTL and never retry.
                    if content:
                        _source_cache[cache_key] = (now, content)
                except Exception:
                    results[ref_key_str] = None

    return results
