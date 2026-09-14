"""OpenAlex fallback leg: sits between the arXiv export API and Semantic
Scholar, applies the same title guard, and follows the matched work's arXiv
location (arxiv.org main host stays up when the export API 429s).
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "web"))

import citation_resolver as cr


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _work(title, arxiv_id=None):
    locs = []
    if arxiv_id:
        locs = [{"landing_page_url": f"https://arxiv.org/abs/{arxiv_id}",
                 "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}"}]
    return {"display_name": title, "locations": locs}


def test_openalex_rejects_mismatched_title(monkeypatch):
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)
    fetched = []
    monkeypatch.setattr(cr, "_fetch_arxiv", lambda aid: fetched.append(aid) or "text")
    monkeypatch.setattr(cr.requests, "get", lambda url, **kw: _Resp(
        payload={"results": [_work("Suicide prevention competencies among Muslim faith leaders", "2501.00001")]}))
    assert cr._search_openalex("Attention is all you need") is None
    assert not fetched  # title guard fired before any fetch


def test_openalex_follows_arxiv_location_on_match(monkeypatch):
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)
    monkeypatch.setattr(cr, "_fetch_arxiv", lambda aid: f"full text of {aid}")
    monkeypatch.setattr(cr.requests, "get", lambda url, **kw: _Resp(
        payload={"results": [_work("Attention Is All You Need", "1706.03762")]}))
    assert cr._search_openalex("Attention is all you need") == "full text of 1706.03762"


def test_openalex_ignores_match_without_arxiv_location(monkeypatch):
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)
    monkeypatch.setattr(cr, "_fetch_arxiv", lambda aid: "text")
    monkeypatch.setattr(cr.requests, "get", lambda url, **kw: _Resp(
        payload={"results": [_work("Attention Is All You Need")]}))  # venue-only
    assert cr._search_openalex("Attention is all you need") is None


def test_openalex_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)
    monkeypatch.setattr(cr.requests, "get", lambda url, **kw: _Resp(status_code=429))
    assert cr._search_openalex("Attention is all you need") is None


def test_chain_order_openalex_before_semantic_scholar(monkeypatch):
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)
    monkeypatch.setattr(cr, "_search_arxiv_by_title", lambda q: None)
    monkeypatch.setattr(cr, "_search_openalex", lambda q: "openalex text")
    monkeypatch.setattr(cr, "_search_semantic_scholar",
                        lambda q: (_ for _ in ()).throw(AssertionError("S2 must not run after an OpenAlex hit")))
    assert cr._search_and_fetch("Attention is all you need") == "openalex text"


def test_chain_falls_through_openalex_to_semantic_scholar(monkeypatch):
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)
    monkeypatch.setattr(cr, "_search_arxiv_by_title", lambda q: None)
    monkeypatch.setattr(cr, "_search_openalex", lambda q: None)
    monkeypatch.setattr(cr, "_search_semantic_scholar", lambda q: "s2 text")
    assert cr._search_and_fetch("some title") == "s2 text"
