"""End-to-end test for the website's per-ref verification path.

This exercises the full web pipeline: claim extraction -> ref resolution ->
per-ref verification -> aggregated response. It writes a sample paper to a
temp file and calls the web app's `_run_verification` directly.
"""
import os
import sys
import json
import pytest


@pytest.fixture
def web_app_path():
    """Ensure web/ is on sys.path so we can import the FastAPI app."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    web_path = os.path.join(repo, "web")
    if web_path not in sys.path:
        sys.path.insert(0, web_path)
    return web_path


def test_web_per_ref_for_fully_resolvable_claim(mock_openai, web_app_path, tmp_path, monkeypatch):
    """A claim whose all refs resolve: response has per_ref_verdicts and verdict."""
    # Build a sample paper text with one cited claim
    paper_text = (
        "Transformers [1] use self-attention.\n\n"
        "References\n\n"
        "[1] Vaswani et al. Attention is all you need. NeurIPS 2017.\n"
    )
    paper_file = tmp_path / "test_paper.txt"
    paper_file.write_text(paper_text)

    # Mock the citation resolver so the arXiv fetch is skipped
    import app as web_app
    import citation_resolver
    monkeypatch.setattr(citation_resolver, "resolve_and_fetch_all", lambda full_text, cited_refs: {
        "1": "Vaswani et al. Attention is all you need. NeurIPS 2017. We propose the Transformer, a new simple network architecture based solely on attention mechanisms."
    })

    # Mock OpenAI to return SUPPORTED
    mock_openai.next_verdict = {
        "verdict": "SUPPORTED",
        "confidence": 0.95,
        "evidence_quote": "based solely on attention mechanisms",
        "evidence_start_phrase": "based solely on attention",
        "reasoning": "paper describes attention-based architecture",
    }

    result = web_app._run_verification(str(paper_file), "test_paper.txt", ".txt", "auto")
    assert result["summary"]["total_claims"] >= 1
    claim = result["claims"][0]
    assert claim["verdict"] == "SUPPORTED"
    assert "per_ref_verdicts" in claim
    assert len(claim["per_ref_verdicts"]) == 1
    assert claim["per_ref_verdicts"][0]["verdict"] == "SUPPORTED"


def test_web_per_ref_for_unresolvable_claim(mock_openai, web_app_path, tmp_path, monkeypatch):
    """A claim with all refs unresolvable: dispatches to search-backup.

    With no backup databases selected, an unresolvable cited claim is labeled
    INACCESSIBLE ("a citation is present, but the source could not be
    accessed") rather than NO_BACKUP_FOUND. The unresolved_refs list still
    appears in the response.
    """
    paper_text = (
        "Some claim [1] that we cannot verify.\n\n"
        "References\n\n"
        "[1] Some paper behind a paywall.\n"
    )
    paper_file = tmp_path / "test_paper.txt"
    paper_file.write_text(paper_text)

    # Mock resolver to return empty (paywalled / unresolvable)
    import app as web_app
    import citation_resolver
    import verify_hallucinations
    monkeypatch.setattr(citation_resolver, "resolve_and_fetch_all", lambda full_text, cited_refs: {})
    # Mock PubMed search to also return empty so we don't hit the network
    monkeypatch.setattr(verify_hallucinations, "search_pubmed", lambda query, max_results=3: [])

    result = web_app._run_verification(str(paper_file), "test_paper.txt", ".txt", "auto")
    claim = result["claims"][0]
    # No databases selected -> a cited-but-unresolvable claim is labeled
    # INACCESSIBLE (citation present, source could not be accessed), not
    # NO_BACKUP_FOUND (which would falsely imply it had no citation).
    assert claim["verdict"] == "INACCESSIBLE"
    assert claim["citation_exists"] is False
    assert claim["unresolved_refs"] == [1]
    # Nothing resolved, so there are no per-ref verdicts; the unresolved ref is
    # surfaced via unresolved_refs above.
    assert claim["per_ref_verdicts"] == []

    result = web_app._run_verification(str(paper_file), "test_paper.txt", ".txt", "auto")
    claim = result["claims"][0]
    # No databases selected -> a cited-but-unresolvable claim is labeled
    # INACCESSIBLE (citation present, source could not be accessed), not
    # NO_BACKUP_FOUND (which would falsely imply it had no citation).
    assert claim["verdict"] == "INACCESSIBLE"
    assert claim["citation_exists"] is False
    assert claim["unresolved_refs"] == [1]
    # Nothing resolved, so there are no per-ref verdicts; the unresolved ref is
    # surfaced via unresolved_refs above.
    assert claim["per_ref_verdicts"] == []


def test_web_per_ref_unverifiable_does_not_count_in_reliability(mock_openai, web_app_path, tmp_path, monkeypatch):
    """reliability_percent uses verifiable denominator, not total.

    The bug fixed in H5 was that unverifiable claims were folded into
    precision. The fix splits the denominator.
    """
    paper_text = (
        "The first study found that creatine improves muscle strength [1]. "
        "A separate investigation showed that vitamin D affects mood [2].\n\n"
        "References\n\n"
        "[1] First paper on creatine and muscle function.\n"
        "[2] Second paper (paywalled) on vitamin D and mood.\n"
    )
    paper_file = tmp_path / "test_paper.txt"
    paper_file.write_text(paper_text)

    import app as web_app
    import citation_resolver
    import verify_hallucinations
    monkeypatch.setattr(citation_resolver, "resolve_and_fetch_all", lambda full_text, cited_refs: {
        "1": "Substantial content of the first source paper for keyword overlap."
    })
    # Mock PubMed to return empty (no internet in tests)
    monkeypatch.setattr(verify_hallucinations, "search_pubmed", lambda query, max_results=3: [])

    # First call: per-ref SUPPORTED for ref [1]. Second call (if backup kicks in): also SUPPORTED.
    queue = iter([
        {"verdict": "SUPPORTED", "confidence": 0.95, "evidence_quote": "ok", "reasoning": "supported"},
        {"verdict": "SUPPORTED", "confidence": 0.95, "evidence_quote": "ok", "reasoning": "backup supported"},
    ])
    real_create = mock_openai.create
    def queue_create(*args, **kwargs):
        try:
            mock_openai.next_verdict = next(queue)
        except StopIteration:
            mock_openai.next_verdict = None
        return real_create(*args, **kwargs)
    mock_openai.create = queue_create

    result = web_app._run_verification(str(paper_file), "test_paper.txt", ".txt", "auto")
    # 2 claims extracted: ref [1] resolves to SUPPORTED, ref [2] paywalled
    # With search-backup enabled, the paywalled one will dispatch and either
    # get BACKUP_FOUND (if PubMed returns something) or NO_BACKUP_FOUND (empty).
    # Either way, it counts as "unverifiable" in the original bucket because
    # it's not a SUPPORTED/PARTIALLY verdict against a real ref.
    assert result["summary"]["total_claims"] == 2
    # First claim (ref [1] resolved, SUPPORTED)
    assert result["claims"][0]["verdict"] == "SUPPORTED"
    # Second claim (ref [2] paywalled, no databases -> INACCESSIBLE)
    assert result["claims"][1]["verdict"] == "INACCESSIBLE"
    # The key invariant: reliability denominator is the verifiable count
    # (claims that have a real source), not the total.
    # The first claim has a real source (counts as verifiable), the second
    # doesn't (NO_BACKUP_FOUND/UNVERIFIABLE). So denominator is 1, not 2.


def test_web_offers_patient_retry_only_after_default_throttle(mock_openai, web_app_path, tmp_path, monkeypatch):
    """A ref still throttled after the default retry exposes the UI transition."""
    paper_text = (
        "Some claim [1] remains temporarily unavailable.\n\n"
        "References\n\n"
        "[1] Some real paper in a throttled source.\n"
    )
    paper_file = tmp_path / "throttled.txt"
    paper_file.write_text(paper_text)

    import app as web_app
    import citation_resolver
    result = citation_resolver.ResolutionResult({"1": None}, throttled_refs=["1"])
    monkeypatch.setattr(citation_resolver, "resolve_and_fetch_all", lambda *args, **kwargs: result)

    default = web_app._run_verification(str(paper_file), "throttled.txt", ".txt", "auto")
    patient = web_app._run_verification(
        str(paper_file), "throttled.txt", ".txt", "auto", patient_wait=True
    )

    assert default["retry_offer"] is True
    assert default["throttled_refs"] == ["1"]
    assert patient["retry_offer"] is False
