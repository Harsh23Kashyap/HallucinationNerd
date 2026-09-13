"""Tests: title-match relevance guard on the PubMed / Semantic Scholar fallbacks.

The arXiv title-search path already rejects top hits whose title does not match
the reference (_title_matches). The PubMed and S2 fallbacks did not, so when
arXiv rate-limited (observed 429s), refs silently "resolved" to unrelated
papers - e.g. an ELMo citation fetched a 2026 Transformer-GNN news-transcription
article, and an Attention Is All You Need citation fetched a suicide-prevention
study. These tests pin the guard on both fallbacks.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "web"))

import citation_resolver as cr


class _Resp:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


def _no_arxiv_no_s2(monkeypatch):
    monkeypatch.setattr(cr, "_search_arxiv_by_title", lambda q: None)
    monkeypatch.setattr(cr, "_search_semantic_scholar", lambda q: None)
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)


def test_pubmed_fallback_rejects_mismatched_title(monkeypatch):
    """A PubMed top hit whose title does not match the reference is rejected."""
    _no_arxiv_no_s2(monkeypatch)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "esearch" in url:
            return _Resp(payload={"esearchresult": {"idlist": ["12345"]}})
        if "esummary" in url:
            return _Resp(payload={"result": {"12345": {
                "title": "Suicide prevention competencies among Muslim faith leaders"}}})
        if "efetch" in url:
            return _Resp(text="abstract that should never be fetched " * 5)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(cr.requests, "get", fake_get)
    assert cr._search_and_fetch("Attention is all you need") is None
    assert not any("efetch" in u for u in calls)  # abstract never fetched


def test_pubmed_fallback_accepts_matched_title(monkeypatch):
    """A PubMed hit whose title matches the reference is returned."""
    _no_arxiv_no_s2(monkeypatch)

    def fake_get(url, **kwargs):
        if "esearch" in url:
            return _Resp(payload={"esearchresult": {"idlist": ["999"]}})
        if "esummary" in url:
            return _Resp(payload={"result": {"999": {
                "title": "Deep contextualized word representations"}}})
        if "efetch" in url:
            return _Resp(text="Peters et al. Deep contextualized word representations. " * 5)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(cr.requests, "get", fake_get)
    out = cr._search_and_fetch("Deep contextualized word representations")
    assert out is not None and "Deep contextualized word representations" in out


def test_pubmed_fallback_fails_open_when_title_lookup_fails(monkeypatch):
    """esummary failure must not cost a legitimately matched abstract."""
    _no_arxiv_no_s2(monkeypatch)

    def fake_get(url, **kwargs):
        if "esearch" in url:
            return _Resp(payload={"esearchresult": {"idlist": ["77"]}})
        if "esummary" in url:
            return _Resp(status_code=500)
        if "efetch" in url:
            return _Resp(text="Some biomedical abstract content here. " * 5)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(cr.requests, "get", fake_get)
    assert cr._search_and_fetch("Some biomedical study") is not None


def test_semantic_scholar_rejects_mismatched_title(monkeypatch):
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)

    def fake_get(url, **kwargs):
        assert "semanticscholar" in url
        return _Resp(payload={"data": [{
            "title": "Drug discovery from gene expression and gene regulation",
            "abstract": "Unrelated biomedical abstract.",
            "externalIds": {}}]})

    monkeypatch.setattr(cr.requests, "get", fake_get)
    assert cr._search_semantic_scholar(
        "Improving language understanding by generative pre-training") is None


def test_semantic_scholar_accepts_matched_title(monkeypatch):
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)

    def fake_get(url, **kwargs):
        assert "semanticscholar" in url
        return _Resp(payload={"data": [{
            "title": "Improving Language Understanding by Generative Pre-Training",
            "abstract": "The real abstract.",
            "externalIds": {}}]})

    monkeypatch.setattr(cr.requests, "get", fake_get)
    out = cr._search_semantic_scholar("Improving language understanding by generative pre-training")
    assert out is not None and "The real abstract." in out


def test_guard_tolerates_author_year_noise_in_query(monkeypatch):
    """Raw-reference queries carry author/year noise; the guard must still pass
    when the candidate title is right (same tolerance as the arXiv path)."""
    assert cr._title_matches(
        "Peters Matthew 2018 Deep contextualized word representations",
        "Deep contextualized word representations")
    assert not cr._title_matches(
        "Vaswani Ashish 2017 Attention is all you need",
        "Suicide prevention competencies among Muslim faith leaders in Canada")
