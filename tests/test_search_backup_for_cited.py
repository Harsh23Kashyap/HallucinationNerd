"""Tests for C3: UNVERIFIABLE for cited-but-paywalled claims dispatches to search-backup.

Dennis's 6 Aug directive: "HallucinationNerd would find hallucinations when
references are given and provide references for unverifiable statements."

The implementation: if all cited refs are UNVERIFIABLE (because paywalled
or unresolvable), dispatch to search-backup. If a backup is found, the
verdict becomes BACKUP_FOUND instead of UNVERIFIABLE.
"""
import json
import pytest


def test_paywalled_ref_dispatches_to_pubmed(mock_openai):
    """When all cited refs are paywalled and search_backup=True, dispatch to PubMed."""
    from verify_hallucinations import verify_claim_per_ref

    # First LLM call: per-ref evaluation (ref 1 paywalled, content empty -> UNVERIFIABLE)
    # Second LLM call: PubMed search verification (mocked to return SUPPORTED)
    queue = iter([
        # Per-ref evaluation: ref 1 has content, LLM says UNVERIFIABLE
        {"verdict": "UNVERIFIABLE", "confidence": 0.0, "evidence_quote": "", "reasoning": "paywalled"},
        # PubMed search: 1 result, the LLM verifies it supports the claim
        {"verdict": "SUPPORTED", "confidence": 0.85, "evidence_quote": "backup evidence", "reasoning": "backup supports claim"},
    ])
    real_create = mock_openai.create
    def queue_create(*args, **kwargs):
        mock_openai.next_verdict = next(queue)
        return real_create(*args, **kwargs)
    mock_openai.create = queue_create

    class _Found:
        def get(self, k, default=""):
            return {"title": "Backup paper", "content": "Backup content supporting the claim.", "url": "http://x"}.get(k, default)
    class _SearchFn:
        def __call__(self, q):
            return [_Found()]

    claim = {"claim_text": "Some claim that needs backup [1].", "cited_refs": [1]}
    articles = [{"content": "Some short paywalled content."}]  # content exists but LLM says UNVERIFIABLE
    result = verify_claim_per_ref(
        claim, articles, question="Q", question_id="Q1", claim_idx=1,
        search_backup=True, search_fn=_SearchFn(),
    )
    assert result.verdict == "BACKUP_FOUND"
    assert "backup" in result.evidence_reference.lower() or result.evidence_reference.startswith("BACKUP:")


def test_no_resolvable_ref_dispatches_to_pubmed(mock_openai):
    """When no cited ref is resolvable at all, dispatch to PubMed."""
    from verify_hallucinations import verify_claim_per_ref

    # Only LLM call: PubMed search verification
    mock_openai.next_verdict = {
        "verdict": "SUPPORTED",
        "confidence": 0.8,
        "evidence_quote": "backup",
        "reasoning": "found backup",
    }
    class _Found:
        def get(self, k, default=""):
            return {"title": "Backup", "content": "Backup content.", "url": "http://x"}.get(k, default)
    class _SearchFn:
        def __call__(self, q):
            return [_Found()]

    claim = {"claim_text": "Uncited... wait, cited but unresolvable.", "cited_refs": [1]}
    articles = []  # no resolvable content at all
    result = verify_claim_per_ref(
        claim, articles, question="Q", question_id="Q1", claim_idx=1,
        search_backup=True, search_fn=_SearchFn(),
    )
    assert result.verdict == "BACKUP_FOUND"


def test_search_backup_disabled_keeps_unverifiable(mock_openai):
    """Without search_backup, paywalled cited claims stay UNVERIFIABLE."""
    from verify_hallucinations import verify_claim_per_ref

    mock_openai.next_verdict = {
        "verdict": "UNVERIFIABLE", "confidence": 0.0, "evidence_quote": "", "reasoning": "paywalled"
    }
    claim = {"claim_text": "Some claim [1].", "cited_refs": [1]}
    articles = [{"content": "Short paywalled content."}]
    result = verify_claim_per_ref(
        claim, articles, question="Q", question_id="Q1", claim_idx=1,
        search_backup=False,  # disabled
    )
    assert result.verdict == "UNVERIFIABLE"


def test_supported_ref_skips_search_backup(mock_openai):
    """A SUPPORTED ref means search-backup should not be triggered."""
    from verify_hallucinations import verify_claim_per_ref

    mock_openai.next_verdict = {
        "verdict": "SUPPORTED", "confidence": 0.9, "evidence_quote": "substantial content", "reasoning": "ok"
    }
    claim = {"claim_text": "Some claim [1].", "cited_refs": [1]}
    articles = [{"content": "This is a substantial content for the source paper."}]
    result = verify_claim_per_ref(
        claim, articles, question="Q", question_id="Q1", claim_idx=1,
        search_backup=True,
    )
    # Search-backup should NOT have been called (only 1 LLM call, not 2)
    assert result.verdict == "SUPPORTED"
    assert mock_openai.call_count == 1
