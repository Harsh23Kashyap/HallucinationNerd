"""Bounded retries for resolver HTTP requests."""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "web"))

import citation_resolver as cr


class _Resp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def test_retries_429_then_returns_success(monkeypatch):
    responses = iter([_Resp(429), _Resp(429), _Resp(200)])
    sleeps = []
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)
    monkeypatch.setattr(cr.requests, "get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(cr._time, "sleep", sleeps.append)

    response = cr._get_with_backoff("https://example.test", timeout=1)

    assert response.status_code == 200
    assert sleeps == [0.6, 1.2]


def test_uses_bounded_retry_after(monkeypatch):
    responses = iter([_Resp(429, {"Retry-After": "30"}), _Resp(200)])
    sleeps = []
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)
    monkeypatch.setattr(cr.requests, "get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(cr._time, "sleep", sleeps.append)

    assert cr._get_with_backoff("https://example.test").status_code == 200
    assert sleeps == [cr._MAX_RETRY_DELAY_SECONDS]


def test_stops_after_max_attempts(monkeypatch):
    calls = []
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)
    monkeypatch.setattr(cr.requests, "get", lambda *args, **kwargs: calls.append(1) or _Resp(429))
    monkeypatch.setattr(cr._time, "sleep", lambda _: None)

    response = cr._get_with_backoff("https://example.test")

    assert response.status_code == 429
    assert len(calls) == cr._MAX_REQUEST_ATTEMPTS


def test_does_not_retry_permanent_error(monkeypatch):
    calls = []
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)
    monkeypatch.setattr(cr.requests, "get", lambda *args, **kwargs: calls.append(1) or _Resp(404))
    monkeypatch.setattr(cr._time, "sleep", lambda _: (_ for _ in ()).throw(AssertionError("slept")))

    response = cr._get_with_backoff("https://example.test")

    assert response.status_code == 404
    assert len(calls) == 1


def test_retries_request_exception_then_returns_success(monkeypatch):
    calls = []
    sleeps = []

    def get(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise cr.requests.Timeout("timed out")
        return _Resp(200)

    monkeypatch.setattr(cr, "_rate_limit", lambda: None)
    monkeypatch.setattr(cr.requests, "get", get)
    monkeypatch.setattr(cr._time, "sleep", sleeps.append)

    response = cr._get_with_backoff("https://example.test")

    assert response.status_code == 200
    assert len(calls) == 2
    assert sleeps == [0.6]
