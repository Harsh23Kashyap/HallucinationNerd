"""Opt-in arXiv identifier screening. No model, source PDF or title-search calls.

This is deliberately separate from claim-support verification. UNKNOWN is not
proof of nonexistence. The shared limiter/cache are process-local; deployments
with multiple workers need a shared provider limiter before enabling this mode.
"""
import json
import re
import threading
import time
from collections import OrderedDict, Counter
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

ENDPOINT = 'https://export.arxiv.org/api/query'
ID_PATTERN = r'(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?'
ID = re.compile(ID_PATTERN + r'\Z')
MENTION = re.compile(r'(?:arXiv\s*:\s*|https?://arxiv\.org/abs/|\babs/)(' + ID_PATTERN + r')(?![\w./])', re.I)
ATOM = '{http://www.w3.org/2005/Atom}'
MAX_IDS = 100
MAX_ROWS = 1000
MAX_RESPONSE = 5_000_000
BUDGET = 60
CACHE_TTL = 86400
CACHE_LIMIT = 2000
_provider_lock = threading.Lock()
_cache = OrderedDict()
_next_request = 0.0


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    for prefix in ('https://arxiv.org/abs/', 'http://arxiv.org/abs/', 'arXiv:'):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value if ID.fullmatch(value) else None


def _candidate(marker, values):
    ids = sorted({i for v in values if (i := normalize(v))})
    return {'marker': str(marker), 'arxiv_id': ids[0] if len(ids) == 1 else None,
            'status': 'UNKNOWN', 'reason': 'not_looked_up' if len(ids) == 1 else
            'ambiguous_identifiers' if ids else 'missing_or_unsupported_identifier',
            'claim_support_status': 'NOT_CHECKED',
            'bibliography_identity_status': 'NOT_CHECKED'}


def extract_rows(path, suffix):
    """Conservative parsing with explicit coverage warnings, never LLM fallback."""
    warnings = []
    if suffix == '.json':
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        entries = data if isinstance(data, list) else [data]
        rows = []
        for number, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                raise ValueError('Expected structured objects containing retrieved_articles')
            sources = entry.get('retrieved_articles')
            if not isinstance(sources, list):
                raise ValueError('Expected a retrieved_articles list in every structured entry')
            for index, source in enumerate(sources or [{}], 1):
                if not isinstance(source, dict):
                    raise ValueError('Expected source objects in retrieved_articles')
                rows.append(_candidate(f'{number}.{index}', [source.get('id'), source.get('arxiv_id')]))
                if len(rows) >= MAX_ROWS:
                    warnings.append('Reference row limit reached; remaining input was not screened.')
                    return rows, warnings
        warnings.append('Coverage is supplied source occurrences, not extracted inline citations.')
        return rows, warnings
    if suffix == '.pdf':
        import fitz
        with fitz.open(path) as document:
            if document.page_count > 300:
                raise ValueError('ID screening supports at most 300 PDF pages')
            chunks = []
            size = 0
            for page in document:
                chunk = page.get_text()
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError('Extracted text exceeds ID screening limit')
                chunks.append(chunk)
            text = '\n'.join(chunks)
    else:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
        if suffix in ('.html', '.htm'):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, 'html.parser')
            for tag in soup(['script', 'style']):
                tag.decompose()
            text = soup.get_text(separator='\n')
    if len(text) > 2_000_000:
        raise ValueError('Extracted text exceeds ID screening limit')
    section = re.search(r'(?:^|\n)\s*(?:references?|bibliography|works cited)\s*\n', text, re.I)
    if not section:
        return [], ['No explicit bibliography heading found. No identifiers looked up; coverage is incomplete.']
    bibliography = text[section.end():]
    trailing = re.search(r'(?:^|\n)\s*(?:appendix|response to reviewers?|author contributions)\b', bibliography, re.I)
    if trailing:
        bibliography = bibliography[:trailing.start()]
    starts = list(re.finditer(r'(?:^|\n)\s*(?:\[(\d{1,4})\]|(\d{1,4})\.)\s+', bibliography))
    if not starts:
        return [], ['Only numbered bibliography entries are supported in this first step. Coverage is incomplete.']
    rows = []
    seen_markers = set()
    for index, match in enumerate(starts[:MAX_ROWS]):
        marker = match.group(1) or match.group(2)
        end = starts[index + 1].start() if index + 1 < len(starts) else len(bibliography)
        raw = bibliography[match.end():end]
        row = _candidate(marker, MENTION.findall(raw))
        if marker in seen_markers:
            row.update(arxiv_id=None, reason='duplicate_bibliography_marker')
            for previous in rows:
                if previous['marker'] == marker:
                    previous.update(arxiv_id=None, reason='duplicate_bibliography_marker')
        seen_markers.add(marker)
        rows.append(row)
    warnings.append('Coverage is parsed numbered bibliography entries, not inline citation occurrences. Extraction may be incomplete; author-year and scanned/OCR references are not supported.')
    if len(starts) > MAX_ROWS:
        warnings.append('Reference row limit reached; remaining entries were not screened.')
    return rows, warnings


def parse_metadata(payload, requested):
    if b'<!DOCTYPE' in payload.upper() or b'<!ENTITY' in payload.upper():
        raise ValueError('Unexpected XML declarations')
    root = ET.fromstring(payload)
    if root.tag != ATOM + 'feed':
        raise ValueError('Expected Atom metadata')
    found = {}
    for entry in root.findall(ATOM + 'entry'):
        ident = normalize(entry.findtext(ATOM + 'id', ''))
        title = ' '.join(entry.findtext(ATOM + 'title', '').split())
        if not ident or not title or title.lower() == 'error':
            continue
        for requested_id in requested:
            if ident == requested_id or ('v' not in requested_id and re.sub(r'v\d+$', '', ident) == requested_id):
                found[requested_id] = {'resolved_id': ident, 'title': title[:1000],
                                       'source_url': 'https://arxiv.org/abs/' + ident,
                                       'looked_up_at': utc_now()}
    return found


def resolve_ids(ids, *, cancel=None, budget=BUDGET):
    """One request at a time per process; positive cache only; never retries."""
    global _next_request
    cancel = cancel or threading.Event()
    deadline = time.monotonic() + budget
    found, reasons, requests_made = {}, {}, 0
    ids = sorted(set(ids))
    # Waiting for a shared provider lock is also inside the request budget.
    while not _provider_lock.acquire(timeout=min(0.1, max(0.001, deadline - time.monotonic()))):
        if cancel.is_set() or time.monotonic() >= deadline:
            return {}, {i: 'cancelled_or_budget_exhausted' for i in ids}, 0
    try:
        now = time.monotonic()
        for key in list(_cache):
            if now - _cache[key][0] >= CACHE_TTL:
                del _cache[key]
        pending = []
        for ident in ids:
            if ident in _cache:
                found[ident] = dict(_cache[ident][1], cache_hit=True)
                _cache.move_to_end(ident)
            else:
                pending.append(ident)
        for start in range(0, len(pending), 50):
            remaining = pending[start:]
            wait = max(0.0, _next_request - time.monotonic())
            if cancel.is_set() or time.monotonic() + wait >= deadline:
                reasons.update({i: 'cancelled_or_budget_exhausted' for i in remaining})
                break
            if cancel.wait(wait):
                reasons.update({i: 'cancelled_or_budget_exhausted' for i in remaining})
                break
            batch = pending[start:start + 50]
            _next_request = time.monotonic() + 3.1
            requests_made += 1
            try:
                timeout = max(0.001, min(10.0, (deadline - time.monotonic()) / 2))
                with requests.get(ENDPOINT, params={'id_list': ','.join(batch), 'max_results': len(batch)},
                                  headers={'User-Agent': 'HallucinationNerd-citation-screen/0.1'},
                                  timeout=(timeout, timeout), allow_redirects=False, stream=True) as response:
                    if response.status_code == 429:
                        _next_request = time.monotonic() + 60
                        reasons.update({i: 'provider_throttled' for i in remaining})
                        break
                    if response.status_code != 200:
                        reasons.update({i: 'provider_http_error' for i in remaining})
                        break
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if cancel.is_set() or time.monotonic() >= deadline:
                            raise TimeoutError('Screening budget exhausted')
                        if size > MAX_RESPONSE:
                            raise ValueError('Metadata response too large')
                        chunks.append(chunk)
                    got = parse_metadata(b''.join(chunks), batch)
                for ident in batch:
                    if ident in got:
                        found[ident] = dict(got[ident], cache_hit=False)
                        _cache[ident] = (time.monotonic(), got[ident])
                    else:
                        reasons[ident] = 'no_matching_metadata'
                while len(_cache) > CACHE_LIMIT:
                    _cache.popitem(last=False)
            except (requests.RequestException, ValueError, ET.ParseError, TimeoutError):
                reasons.update({i: 'lookup_failed' for i in remaining})
                break
        return found, reasons, requests_made
    finally:
        _provider_lock.release()


def screen_document(path, filename, suffix, *, offline=False, cancel=None):
    started = time.monotonic()
    rows, warnings = extract_rows(path, suffix)
    identifiers = sorted({r['arxiv_id'] for r in rows if r['arxiv_id']})
    allowed = identifiers[:MAX_IDS]
    found, reasons, calls = ({}, {}, 0) if offline else resolve_ids(allowed, cancel=cancel)
    for row in rows:
        ident = row['arxiv_id']
        if ident in found:
            row.update(found[ident], status='IDENTIFIER_EXISTS', reason='arxiv_metadata_entry')
        elif ident:
            row['reason'] = 'offline_no_external_evidence' if offline else reasons.get(ident, 'identifier_limit')
    return {'schema_version': 1, 'verification_mode': 'citation_existence', 'filename': filename,
            'claim_support_status': 'NOT_CHECKED', 'bibliography_identity_status': 'NOT_CHECKED',
            'coverage_basis': 'supplied_source_occurrences' if suffix == '.json' else 'parsed_numbered_bibliography_entries',
            'extraction_warnings': warnings, 'offline': offline,
            'summary': {'total_rows': len(rows), 'unique_supported_ids': len(identifiers),
                        'identifier_exists': sum(r['status'] == 'IDENTIFIER_EXISTS' for r in rows),
                        'unknown': sum(r['status'] == 'UNKNOWN' for r in rows)},
            'unknown_reasons': dict(Counter(r['reason'] for r in rows if r['status'] == 'UNKNOWN')),
            'requests_made': calls, 'elapsed_seconds': round(time.monotonic() - started, 3),
            'rows': rows}
