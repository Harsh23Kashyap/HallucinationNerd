"""Tests for the never-falsely-accuse gate (negative-verdict full-source recheck).

Failure mode (Oct 14 smoke review): for sources longer than the 4000-char
excerpt budget, _find_relevant_spans shows the judge only keyword-overlap
excerpts. A supporting passage phrased in different vocabulary is never
shown, and the judge returns NOT_SUPPORTED for a claim the source actually
supports. The gate re-shows the unseen regions before accepting a negative
verdict, and downgrades to UNVERIFIABLE when full coverage is impossible.
"""
import json
import pytest

import verify_hallucinations as vh


def _queue(mock_openai, verdicts):
    """Force the mock to return the given verdicts in order."""
    q = iter([
        {"verdict": v, "confidence": 0.9, "evidence_quote": "q" if v == "SUPPORTED" else "",
         "evidence_start_phrase": "q" if v == "SUPPORTED" else "", "reasoning": f"forced {v}"}
        for v in verdicts
    ])
    real_create = mock_openai.create
    def queue_create(*args, **kwargs):
        mock_openai.next_verdict = next(q)
        return real_create(*args, **kwargs)
    mock_openai.create = queue_create


def _long_source(support_text="The architecture attends to every position jointly and in parallel."):
    """A >4000-char source whose keyword-relevant material is all up front,
    with the supporting passage far at the end using different vocabulary."""
    head = "Transformers and self-attention mechanisms are discussed here. " * 30
    filler = "Unrelated filler sentence about gardening and cookware. " * 55
    return head + filler + support_text + " " + filler[:500]


@pytest.fixture(autouse=True)
def _restore_config():
    old = dict(vh.CONFIG)
    yield
    vh.CONFIG.clear()
    vh.CONFIG.update(old)


def test_false_not_supported_overturned_by_recheck(mock_openai):
    """Round 1 excerpt drops the supporting passage -> NOT_SUPPORTED.
    Round 2 shows the unseen region -> SUPPORTED. Final must be SUPPORTED."""
    _queue(mock_openai, ["NOT_SUPPORTED", "SUPPORTED"])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": _long_source(), "title": "Paper"}]
    result = vh.verify_claim_per_ref(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "SUPPORTED"
    assert result.per_ref_verdicts[0]["recheck_rounds"] == 2
    # The second judge call must have received the previously unseen tail
    assert "attends to every position jointly" in mock_openai.calls[1]["user"]


def test_true_negative_accepted_after_full_coverage(mock_openai):
    """Both rounds negative and coverage complete -> NOT_SUPPORTED stands."""
    _queue(mock_openai, ["NOT_SUPPORTED", "NOT_SUPPORTED"])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    content = _long_source()[:4500]  # one complement round covers the rest
    articles = [{"content": content, "title": "Paper"}]
    result = vh.verify_claim_per_ref(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "NOT_SUPPORTED"
    entry = result.per_ref_verdicts[0]
    assert entry["coverage"] >= 0.99
    assert entry["negative_confirmed_full_source"] is True


def test_negative_without_full_coverage_becomes_unverifiable(mock_openai):
    """Round budget exhausted with unseen regions left -> UNVERIFIABLE,
    never an accusation."""
    vh.CONFIG["max_recheck_rounds"] = 1  # no recheck allowed
    _queue(mock_openai, ["NOT_SUPPORTED"])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": _long_source(), "title": "Paper"}]
    result = vh.verify_claim_per_ref(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "UNVERIFIABLE"
    assert "instead of accusing" in result.per_ref_verdicts[0]["reasoning"]
    assert result.per_ref_verdicts[0]["coverage"] < 0.99


def test_contradicted_is_gated_the_same_way(mock_openai):
    """CONTRADICTED is also evidence-location-sensitive and gets the gate."""
    _queue(mock_openai, ["CONTRADICTED", "SUPPORTED"])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": _long_source(), "title": "Paper"}]
    result = vh.verify_claim_per_ref(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "SUPPORTED"


def test_short_source_negative_accepted_without_recheck(mock_openai):
    """Content within the excerpt budget is fully shown in round 1, so a
    negative verdict needs no recheck and no extra calls."""
    _queue(mock_openai, ["NOT_SUPPORTED"])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": "Short paper about something else entirely.", "title": "Paper"}]
    result = vh.verify_claim_per_ref(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "NOT_SUPPORTED"
    assert mock_openai.call_count == 1


def test_gate_disabled_restores_old_behavior(mock_openai):
    vh.CONFIG["full_source_recheck"] = False
    _queue(mock_openai, ["NOT_SUPPORTED"])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": _long_source(), "title": "Paper"}]
    result = vh.verify_claim_per_ref(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "NOT_SUPPORTED"
    assert mock_openai.call_count == 1


def test_single_claim_path_gate(mock_openai):
    """The benchmark path (verify_single_claim) applies the same gate."""
    _queue(mock_openai, ["NOT_SUPPORTED", "SUPPORTED"])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": _long_source(), "title": "Paper"}]
    result = vh.verify_single_claim(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "SUPPORTED"
    assert mock_openai.call_count == 2


def test_single_claim_path_negative_confirmed_full_coverage(mock_openai):
    _queue(mock_openai, ["NOT_SUPPORTED", "NOT_SUPPORTED"])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": _long_source()[:4500], "title": "Paper"}]
    result = vh.verify_single_claim(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "NOT_SUPPORTED"
    assert mock_openai.call_count == 2


def test_coverage_helpers():
    assert vh._merge_ranges([(0, 10), (5, 15), (20, 25)]) == [(0, 15), (20, 25)]
    assert vh._uncovered_ranges(30, [(0, 10), (20, 30)]) == [(10, 20)]
    assert abs(vh._coverage_fraction(100, [(0, 50)]) - 0.5) < 1e-9
    text, used = vh._ranges_to_text("abcdefghij" * 10, [(0, 50), (60, 110)], max_chars=70)
    assert len(text) <= 70
    assert used[0] == (0, 50)


def test_spans_with_coverage_matches_legacy_text():
    """The coverage variant must select exactly the same excerpt as before."""
    content = ("alpha beta gamma delta. " * 400) + "transformers attention " * 50 + ("epsilon zeta. " * 300)
    claim = "transformers use attention"
    legacy = vh._find_relevant_spans(claim, content)
    text, covered = vh._find_relevant_spans_with_coverage(claim, content)
    assert text == legacy
    assert covered and all(0 <= s < e <= len(content) for s, e in covered)
    for s, e in covered:
        assert content[s:e] in text or content[s:e][:50] in text
