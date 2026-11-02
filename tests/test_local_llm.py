"""Local model and private mode: the engine can use a model on this computer, and in private
mode it refuses a remote model and never sends claim text to an online search."""
import os
import sys

import pytest

import verify_hallucinations as vh

ENV = ("HVE_LLM_BASE_URL", "HVE_PRIVATE", "VERIFICATION_MODEL", "OPENAI_API_KEY", "HVE_LLM_API_KEY")


@pytest.fixture
def clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(vh, "client", None)
    return monkeypatch


@pytest.mark.parametrize("url,local", [
    ("http://127.0.0.1:11434/v1", True), ("http://localhost:11434/v1", True),
    ("http://[::1]:11434/v1", True), ("http://127.0.0.5:8000", True),
    ("https://api.openai.com/v1", False), ("http://10.0.0.5:11434/v1", False),
    ("http://localhost.evil.com/v1", False), ("", False), (None, False)])
def test_loopback_detection(url, local):
    assert vh._is_loopback_url(url) is local


def test_openai_still_works_by_default(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "sk-test")
    assert vh.llm_config_error() is None


def test_missing_key_is_reported(clean_env):
    assert "OPENAI_API_KEY" in vh.llm_config_error()


def test_local_model_needs_no_openai_key_but_a_model_name(clean_env):
    clean_env.setenv("HVE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    assert "VERIFICATION_MODEL" in vh.llm_config_error()
    clean_env.setenv("VERIFICATION_MODEL", "qwen2.5:14b")
    assert vh.llm_config_error() is None


@pytest.mark.parametrize("base", [None, "https://api.openai.com/v1", "http://10.0.0.5:11434/v1"])
def test_private_mode_refuses_a_model_off_this_computer(clean_env, base):
    clean_env.setenv("HVE_PRIVATE", "1")
    clean_env.setenv("OPENAI_API_KEY", "sk-test")
    clean_env.setenv("VERIFICATION_MODEL", "m")
    if base:
        clean_env.setenv("HVE_LLM_BASE_URL", base)
    assert "Private mode" in vh.llm_config_error()
    with pytest.raises(ValueError):
        vh.get_client()


def test_client_points_at_the_local_server(clean_env):
    seen = {}

    class FakeOpenAI:
        def __init__(self, **kw):
            seen.update(kw)

    clean_env.setattr(vh, "OpenAI", FakeOpenAI)
    clean_env.setenv("HVE_PRIVATE", "1")
    clean_env.setenv("HVE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    clean_env.setenv("VERIFICATION_MODEL", "qwen2.5:14b")
    vh.get_client()
    assert seen["base_url"] == "http://127.0.0.1:11434/v1"


def test_private_mode_never_searches_with_the_claim_text(clean_env):
    clean_env.setenv("HVE_PRIVATE", "1")
    calls = []
    r = vh._search_backup_reference("Our model reaches 91.2 on GLUE.", "q", "Q1", 0,
                                    lambda q: calls.append(q) or [])
    assert calls == [] and r.verdict == "UNVERIFIABLE"
    assert "Private mode" in r.reasoning


@pytest.fixture
def web_client(clean_env):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo, "web"))
    from fastapi.testclient import TestClient
    import app as web_app
    clean_env.setenv("HVE_PRIVATE", "1")
    clean_env.setenv("HVE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    clean_env.setenv("VERIFICATION_MODEL", "qwen2.5:14b")
    return TestClient(web_app.app)


def test_web_page_hides_backup_search_in_private_mode(web_client):
    page = web_client.get("/").text
    assert "Private mode is on" in page and 'name="custom_database"' not in page


def test_web_refuses_backup_search_in_private_mode(web_client):
    r = web_client.post("/verify", files={"file": ("p.txt", b"text", "text/plain")},
                        data={"databases": "pubmed"})
    assert r.status_code == 400 and "Private mode" in r.json()["detail"]
