"""Reference files supplied by the user, and offline mode (nothing looked up online)."""
import os
import sys

import pytest

import verify_hallucinations as vh  # the root engine; import before web/ joins the path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(REPO, "web"))

from local_references import number_from_filename, match_uploaded_references  # noqa: E402

PAPER = (
    "Transformers [1] use self-attention only.\n\n"
    "Residual connections [2] ease the training of deep networks.\n\n"
    "References\n\n"
    "[1] Vaswani et al. Attention is all you need. NeurIPS 2017.\n"
    "[2] He et al. Deep residual learning for image recognition. CVPR 2016.\n"
)
REF1 = ("Attention Is All You Need. We propose the Transformer, a simple network architecture "
        "based solely on attention mechanisms.")


@pytest.mark.parametrize("name,num", [
    ("3.pdf", "3"), ("[12] Smith 2020.pdf", "12"), ("ref3.pdf", "3"), ("Ref_7.txt", "7"),
    ("reference-4.pdf", "4"), ("smith2020.pdf", None), ("attention.pdf", None), ("", None)])
def test_number_from_filename(name, num):
    assert number_from_filename(name) == num


REFS = {"1": {"title": "Attention is all you need"},
        "2": {"title": "Deep residual learning for image recognition"},
        "3": {"title": "Deep residual learning for image recognition at scale"}}


def test_match_by_number_then_by_title():
    matched, names, unmatched = match_uploaded_references(
        REFS, [("1.pdf", "x"), ("paper.pdf", "Attention Is All You Need\nAbstract ...")])
    assert names == {} and unmatched == ["1.pdf", "paper.pdf"]  # duplicate proposals are ambiguous
    matched, names, unmatched = match_uploaded_references(
        REFS, [("paper.pdf", "Attention Is All You Need\nAbstract ...")])
    assert names == {"1": "paper.pdf"} and unmatched == []


def test_ambiguous_title_is_not_guessed():
    # A complete title matches; a prefix of the longer title is not a full match.
    m, names, un = match_uploaded_references(REFS, [("he.pdf", "Deep Residual Learning for Image Recognition")])
    assert names == {"2": "he.pdf"}
    # a file whose text matches no title is left unmatched
    m, names, un = match_uploaded_references(REFS, [("other.pdf", "A study of something else entirely")])
    assert names == {} and un == ["other.pdf"]


@pytest.fixture
def web_app(mock_openai, monkeypatch):
    import app as web_app
    import citation_resolver
    calls = []
    monkeypatch.setattr(citation_resolver, "resolve_and_fetch_all",
                        lambda text, refs, patient=False: calls.append(list(refs)) or {})
    mock_openai.next_verdict = {"verdict": "SUPPORTED", "confidence": 0.9,
                                "evidence_quote": "based solely on attention mechanisms",
                                "evidence_start_phrase": "based solely on attention",
                                "reasoning": "stated in the source"}
    web_app._fetch_calls = calls
    return web_app


def _run(web_app, tmp_path, uploads):
    paper = tmp_path / "paper.txt"
    paper.write_text(PAPER)
    return web_app._run_verification(str(paper), "paper.txt", ".txt", "auto", uploaded_refs=uploads)


def test_offline_mode_fetches_nothing(web_app, tmp_path, monkeypatch):
    monkeypatch.setenv("HVE_OFFLINE", "1")
    r = _run(web_app, tmp_path, [("1.txt", REF1)])
    assert web_app._fetch_calls == []
    rf = r["reference_files"]
    assert rf["offline"] and rf["matched"] == {"1": "1.txt"} and rf["not_supplied"] == ["2"]
    by_ref = {tuple(str(x) for x in c.get("cited_refs", [])): c["verdict"] for c in r["claims"]}
    assert by_ref[("1",)] == "SUPPORTED" and by_ref[("2",)] == "INACCESSIBLE"


def test_uploaded_files_replace_only_their_own_fetches(web_app, tmp_path, monkeypatch):
    monkeypatch.delenv("HVE_OFFLINE", raising=False)
    _run(web_app, tmp_path, [("1.txt", REF1)])
    assert web_app._fetch_calls == [["2"]]


def test_offline_mode_needs_a_local_model(monkeypatch):
    for k in ("HVE_LLM_BASE_URL", "HVE_PRIVATE", "VERIFICATION_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HVE_OFFLINE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert "Private mode" in vh.llm_config_error()


def test_offline_mode_does_not_fetch_reference_urls(monkeypatch):
    monkeypatch.setenv("HVE_OFFLINE", "1")
    monkeypatch.setattr(vh, "_fetch_url", lambda url: pytest.fail("fetched " + url))
    vh.parse_reference({"id": "1", "url": "https://example.org/paper"})


def test_short_form_reference_is_matched_by_its_title():
    import citation_resolver
    refs = citation_resolver.resolve_references(PAPER)
    m, names, un = match_uploaded_references(
        {str(k): v for k, v in refs.items()},
        [("vaswani.pdf", "Some licence line.\nAttention Is All You Need\nAshish Vaswani")])
    assert names == {"1": "vaswani.pdf"}


def test_partial_title_is_not_a_match():
    _, names, unmatched = match_uploaded_references(
        {"1": {"title": "Deep residual learning for image recognition"}},
        [("wrong.txt", "Deep residual learning for image classification")])
    assert names == {} and unmatched == ["wrong.txt"]


def test_duplicate_numbers_are_not_order_dependent():
    uploads = [("1.txt", "paper A"), ("ref1.txt", "paper B")]
    for order in (uploads, uploads[::-1]):
        matched, names, unmatched = match_uploaded_references(REFS, order)
        assert matched == {} and names == {} and len(unmatched) == 2


def test_shared_complete_title_is_ambiguous():
    refs = {"1": {"title": "Deep residual learning"},
            "2": {"title": "Deep residual learning for image recognition"}}
    assert match_uploaded_references(refs, [("paper.txt", "Deep residual learning for image recognition")])[0] == {}


def test_numbered_file_is_not_reassigned_by_title():
    assert match_uploaded_references(REFS, [("99.txt", REF1)])[0] == {}


def test_offline_skips_optional_nli(web_app, tmp_path, monkeypatch):
    import hve_nli
    monkeypatch.setenv("HVE_OFFLINE", "1")
    monkeypatch.setenv("HVE_NLI_ENABLED", "1")
    monkeypatch.setattr(hve_nli, "maybe_short_circuit", lambda *a: pytest.fail("NLI might download weights"))
    _run(web_app, tmp_path, [("1.txt", REF1)])


def test_offline_home_has_no_remote_fonts(web_app, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HVE_OFFLINE", "1")
    for path in ("/", "/about"):
        page = TestClient(web_app.app).get(path)
        assert page.status_code == 200
        assert "fonts.googleapis.com" not in page.text
        assert "fonts.gstatic.com" not in page.text


def test_cached_client_cannot_bypass_offline_config(monkeypatch):
    monkeypatch.setattr(vh, "client", object())
    monkeypatch.setenv("HVE_OFFLINE", "1")
    monkeypatch.setenv("HVE_LLM_BASE_URL", "https://example.org/v1")
    with pytest.raises(ValueError, match="Private mode"):
        vh.get_client()


def test_cached_remote_client_is_replaced_in_offline_mode(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(vh, "client", SimpleNamespace(base_url="https://api.openai.com/v1"))
    monkeypatch.setenv("HVE_OFFLINE", "1")
    monkeypatch.setenv("HVE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("VERIFICATION_MODEL", "test-local")
    local = vh.get_client()
    assert str(local.base_url).startswith("http://127.0.0.1:11434/")
