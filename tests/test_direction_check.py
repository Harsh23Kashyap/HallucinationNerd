"""Direction-reversed claims must never be credited as supported.

Motivating failure (pre-fix live run): the judge (n_votes=1) returned
SUPPORTED/PARTIALLY_SUPPORTED for claims whose core direction was the reverse
of what the cited source states - e.g., claim "X increases Y" where the source
reports X decreases Y. Topic and quantities matched, so the judge over-credited.

Fix: (1) the rubric carries an explicit DIRECTION CHECK; (2) the judge returns
a structured `directions_opposite` flag; (3) a deterministic per-vote guard in
_judge_claim_once corrects an internally inconsistent supportive verdict to
CONTRADICTED before any vote counting. The guard is backward-compatible:
responses without the field are untouched.
"""
import json

import verify_hallucinations as vh


def _payload(verdict, **extra):
    p = {
        "verdict": verdict,
        "confidence": 0.9,
        "evidence_quote": "values fell by half",
        "evidence_start_phrase": "values fell by half",
        "reasoning": "mock",
    }
    p.update(extra)
    return json.dumps(p)


def test_rubric_carries_direction_check():
    assert "DIRECTION CHECK" in vh.CITATION_VERIFICATION_PROMPT
    assert "directions_opposite" in vh.CITATION_VERIFICATION_PROMPT


def test_supported_with_opposite_direction_becomes_contradicted(mock_openai):
    mock_openai.next_json = _payload("SUPPORTED", directions_opposite=True)
    verdict, payload, votes = vh._judge_claim_once("claim: X increases Y", 1)
    assert verdict == "CONTRADICTED"
    assert votes == ["CONTRADICTED"]
    assert "direction guard" in payload["reasoning"]


def test_partial_with_opposite_direction_becomes_contradicted(mock_openai):
    mock_openai.next_json = _payload("PARTIALLY_SUPPORTED", directions_opposite=True)
    verdict, _, votes = vh._judge_claim_once("claim: X increases Y", 1)
    assert verdict == "CONTRADICTED"
    assert votes == ["CONTRADICTED"]


def test_opposite_false_leaves_verdict_unchanged(mock_openai):
    mock_openai.next_json = _payload("SUPPORTED", directions_opposite=False)
    verdict, payload, votes = vh._judge_claim_once("claim: X increases Y", 1)
    assert verdict == "SUPPORTED"
    assert votes == ["SUPPORTED"]
    assert "direction guard" not in payload["reasoning"]


def test_missing_field_is_backward_compatible(mock_openai):
    mock_openai.next_json = _payload("PARTIALLY_SUPPORTED")  # no directions_opposite
    verdict, _, votes = vh._judge_claim_once("claim: X increases Y", 1)
    assert verdict == "PARTIALLY_SUPPORTED"
    assert votes == ["PARTIALLY_SUPPORTED"]


def test_guard_applies_per_vote_before_majority(mock_openai):
    # 3 votes: two supportive-but-opposite, one NOT_SUPPORTED. After the guard
    # the majority is CONTRADICTED, so the guard runs before vote counting.
    mock_openai.next_json = _payload("SUPPORTED", directions_opposite=True)
    v1, p1, _ = vh._judge_claim_once("q", 1)
    mock_openai.next_json = _payload("PARTIALLY_SUPPORTED", directions_opposite=True)
    v2, _, _ = vh._judge_claim_once("q", 1)
    mock_openai.next_json = _payload("NOT_SUPPORTED", directions_opposite=False)
    v3, _, _ = vh._judge_claim_once("q", 1)
    assert (v1, v2, v3) == ("CONTRADICTED", "CONTRADICTED", "NOT_SUPPORTED")


# ─── Aggregation-layer behavior ──────────────────────────────────────────────

def _queued_payload(verdict, **extra):
    p = {
        "verdict": verdict,
        "confidence": 0.9,
        "evidence_quote": "online finetuning improves performance",
        "evidence_start_phrase": "online finetuning improves performance",
        "reasoning": "mock",
    }
    p.update(extra)
    return json.dumps(p)


def _queue_mock(mock_openai, payloads):
    """Serve queued JSON responses in order across internal llm_calls."""
    from tests.conftest import _FakeResponse
    it = iter(payloads)

    def create(model, messages, temperature=None, response_format=None, **kwargs):
        mock_openai.call_count += 1
        return _FakeResponse(next(it))

    mock_openai.create = create


LONG_CONTENT = ("online finetuning improves performance in this setting. " * 400)


def test_aggregation_preserves_direction_guard_contradicted(mock_openai):
    # Round 1: supportive verdict with opposite flag -> guard makes CONTRADICTED.
    # Without the aggregation fix, a later complement-round SUPPORTED would
    # overturn it via strongest-wins.
    _queue_mock(mock_openai, [
        _queued_payload("SUPPORTED", directions_opposite=True),   # round 1 vote
        _queued_payload("SUPPORTED", directions_opposite=False),  # would-be round 2
    ])
    result = vh._verify_against_source(
        "Online finetuning harms decision transformers [1].",
        "question", "1", "Title", LONG_CONTENT, 1, max_chars=1000)
    assert result["verdict"] == "CONTRADICTED"
    assert result["negative_confirmed"] is True
    assert result["rounds"] == 1  # no recheck once the guard has spoken


def test_aggregation_still_upgrades_plain_negative(mock_openai):
    # A plain CONTRADICTED without the direction flag keeps the old
    # strongest-wins behavior: a later SUPPORTED round wins.
    _queue_mock(mock_openai, [
        _queued_payload("CONTRADICTED"),                          # round 1 vote
        _queued_payload("SUPPORTED"),                             # round 2 vote
    ])
    result = vh._verify_against_source(
        "Online finetuning harms decision transformers [1].",
        "question", "1", "Title", LONG_CONTENT, 1, max_chars=1000)
    assert result["verdict"] == "SUPPORTED"
    assert result["rounds"] == 2
