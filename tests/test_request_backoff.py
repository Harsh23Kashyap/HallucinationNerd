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


def _set_attempts(monkeypatch, attempts):
    monkeypatch.setattr(cr._request_policy, "attempts", attempts, raising=False)
    monkeypatch.setattr(cr, "_rate_limit", lambda: None)


def test_default_policy_makes_one_backoff_retry(monkeypatch):
    calls = []
    sleeps = []
    _set_attempts(monkeypatch, cr._DEFAULT_REQUEST_ATTEMPTS)
    monkeypatch.setattr(cr.requests, "get", lambda *a, **k: calls.append(1) or _Resp(429))
    monkeypatch.setattr(cr._time, "sleep", sleeps.append)

    response = cr._get_with_backoff("https://example.test")

    assert response.status_code == 429
    assert len(calls) == 2
    assert sleeps == [0.6]
    assert cr._request_policy.throttled is True


def test_patient_policy_allows_ten_backoff_retries(monkeypatch):
    calls = []
    _set_attempts(monkeypatch, cr._PATIENT_REQUEST_ATTEMPTS)
    monkeypatch.setattr(cr.requests, "get", lambda *a, **k: calls.append(1) or _Resp(429))
    monkeypatch.setattr(cr._time, "sleep", lambda _: None)

    response = cr._get_with_backoff("https://example.test")

    assert response.status_code == 429
    assert len(calls) == 11


def test_patient_policy_can_recover_on_last_attempt(monkeypatch):
    calls = []
    _set_attempts(monkeypatch, cr._PATIENT_REQUEST_ATTEMPTS)

    def get(*args, **kwargs):
        calls.append(1)
        return _Resp(200 if len(calls) == 11 else 429)

    monkeypatch.setattr(cr.requests, "get", get)
    monkeypatch.setattr(cr._time, "sleep", lambda _: None)

    assert cr._get_with_backoff("https://example.test").status_code == 200
    assert len(calls) == 11
    assert cr._request_policy.throttled is False


def test_uses_bounded_retry_after(monkeypatch):
    responses = iter([_Resp(429, {"Retry-After": "30"}), _Resp(200)])
    sleeps = []
    _set_attempts(monkeypatch, cr._DEFAULT_REQUEST_ATTEMPTS)
    monkeypatch.setattr(cr.requests, "get", lambda *a, **k: next(responses))
    monkeypatch.setattr(cr._time, "sleep", sleeps.append)

    assert cr._get_with_backoff("https://example.test").status_code == 200
    assert sleeps == [cr._MAX_RETRY_DELAY_SECONDS]


def test_does_not_retry_permanent_error(monkeypatch):
    calls = []
    _set_attempts(monkeypatch, cr._DEFAULT_REQUEST_ATTEMPTS)
    monkeypatch.setattr(cr.requests, "get", lambda *a, **k: calls.append(1) or _Resp(404))
    monkeypatch.setattr(cr._time, "sleep", lambda _: (_ for _ in ()).throw(AssertionError("slept")))

    assert cr._get_with_backoff("https://example.test").status_code == 404
    assert len(calls) == 1


def test_retries_request_exception_then_returns_success(monkeypatch):
    calls = []
    sleeps = []
    _set_attempts(monkeypatch, cr._DEFAULT_REQUEST_ATTEMPTS)

    def get(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise cr.requests.Timeout("timed out")
        return _Resp(200)

    monkeypatch.setattr(cr.requests, "get", get)
    monkeypatch.setattr(cr._time, "sleep", sleeps.append)

    assert cr._get_with_backoff("https://example.test").status_code == 200
    assert len(calls) == 2
    assert sleeps == [0.6]


def test_resolution_result_exposes_throttled_refs_for_ui(monkeypatch):
    monkeypatch.setattr(cr, "resolve_references", lambda _: {"1": {"url": "https://example.test"}})
    monkeypatch.setattr(cr, "_fetch_with_policy", lambda info, patient: (None, True))
    cr._source_cache.clear()

    result = cr.resolve_and_fetch_all("References\n[1] Example", ["1"])

    assert result == {"1": None}
    assert result.throttled_refs == ["1"]
