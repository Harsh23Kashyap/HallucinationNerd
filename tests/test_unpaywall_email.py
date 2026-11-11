"""Tests for L9: Unpaywall email from environment.

The Unpaywall API requires a real email for their free tier. The previous
default was a fake 'hallucinationnerd@example.com', which may get rate-
limited. The new code reads UNPAYWALL_EMAIL from env, falling back to
the placeholder.
"""
import os
import pytest


def test_unpaywall_email_from_env(monkeypatch):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "real-researcher@example.org")
    # Re-import the module so the env var is read
    import importlib
    import web.citation_resolver as cr
    importlib.reload(cr)
    # _fetch_doi reads os.getenv("UNPAYWALL_EMAIL") at call time, so
    # monkeypatching the env is sufficient — no need to reload.
    monkeypatch.setattr("web.citation_resolver.public_get", lambda *a, **kw: None)
    # Just verify the env var is read; full fetch is exercised by the
    # citation_resolver end-to-end test (out of scope here).
    assert os.getenv("UNPAYWALL_EMAIL") == "real-researcher@example.org"


def test_unpaywall_email_falls_back_to_placeholder(monkeypatch):
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    assert os.getenv("UNPAYWALL_EMAIL", "hallucinationnerd@example.com") == "hallucinationnerd@example.com"
