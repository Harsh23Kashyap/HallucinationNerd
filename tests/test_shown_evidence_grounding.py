"""Tests for the never-falsely-pass grounding check (verify_single_claim).

Failure mode (10+10 review, SCOPE-PD case): for sources longer than the
excerpt budget, the judge saw 3.5k of 43k chars, and returned
PARTIALLY_SUPPORTED while quoting/reasoning from content it was never shown
(parametric knowledge of a well-known dataset). The mirror of the
never-falsely-accuse gate: a verdict must be grounded in an evidence quote
from the chunks of the cited ref actually shown to the judge, else it
downgrades to UNVERIFIABLE. A shown quote is necessary, never sufficient
proof of entailment.
"""
import pytest

import verify_hallucinations as vh


def _queue(mock_openai, payloads):
    q = iter(payloads)
    real_create = mock_openai.create
    def queue_create(*args, **kwargs):
        mock_openai.next_verdict = next(q)
        return real_create(*args, **kwargs)
    mock_openai.create = queue_create


def _long_source():
    """Keyword-relevant material up front; distinct passage far at the end."""
    head = "Transformers and self-attention mechanisms are discussed here. " * 30
    filler = "Unrelated filler sentence about gardening and cookware. " * 55
    tail = "The ablation removes every residual connection before evaluation."
    return head + filler + tail + " " + filler[:500]


def _verdict(v, quote="", reasoning="forced"):
    return {"verdict": v, "confidence": 0.9, "evidence_quote": quote,
            "evidence_start_phrase": " ".join(quote.split()[:5]) if quote else "",
            "reasoning": reasoning}


@pytest.fixture(autouse=True)
def _restore_config():
    old = dict(vh.CONFIG)
    yield
    vh.CONFIG.clear()
    vh.CONFIG.update(old)


def test_supported_with_quote_from_unshown_region_downgrades(mock_openai):
    """SCOPE-PD mechanism: quote is real source text, but from a region the
    judge was never shown -> UNVERIFIABLE, not a pass."""
    _queue(mock_openai, [_verdict(
        "SUPPORTED", "The ablation removes every residual connection before evaluation.")])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": _long_source(), "title": "Paper"}]
    result = vh.verify_single_claim(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "UNVERIFIABLE"
    assert "not grounded" in result.reasoning
    assert result.evidence_quote == ""


def test_supported_without_quote_downgrades(mock_openai):
    """A missing quote must not evade the check."""
    _queue(mock_openai, [_verdict("SUPPORTED", "")])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": "Transformers use self-attention jointly. " * 10, "title": "Paper"}]
    result = vh.verify_single_claim(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "UNVERIFIABLE"
    assert "no evidence quote" in result.reasoning


def test_supported_with_shown_quote_stands(mock_openai):
    """A quote from the shown excerpt keeps the verdict (shown quote is a
    necessary condition, not treated as proof by itself - no upgrade logic)."""
    quote = "Transformers use self-attention jointly."
    _queue(mock_openai, [_verdict("SUPPORTED", quote)])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": quote + " " + "Padding text here. " * 10, "title": "Paper"}]
    result = vh.verify_single_claim(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "SUPPORTED"


def test_quote_whitespace_normalized(mock_openai):
    """Whitespace differences between quote and source must not cause a
    false downgrade."""
    quote = "Transformers   use\nself-attention jointly."
    _queue(mock_openai, [_verdict("SUPPORTED", quote)])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": "Transformers use self-attention jointly. Padding. " * 5, "title": "Paper"}]
    result = vh.verify_single_claim(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "SUPPORTED"


def test_quote_from_other_ref_does_not_ground(mock_openai):
    """Provenance: a quote shown for ref 2 must not ground a verdict about ref 1."""
    _queue(mock_openai, [_verdict("SUPPORTED", "Beta blockers are discussed at length here.")])
    claim = {"claim_text": "Claim about both [1] and [2].", "cited_refs": [1, 2]}
    articles = [
        {"content": "Alpha agonists are described in this short paper.", "title": "Ref1"},
        {"content": "Beta blockers are discussed at length here.", "title": "Ref2"},
    ]
    result = vh.verify_single_claim(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "UNVERIFIABLE"


def test_not_supported_needs_no_quote_but_coverage_gate_still_applies(mock_openai):
    """NOT_SUPPORTED asserts absence: no quote required. With a short source
    (fully shown) it stands; with a long source and no recheck budget it
    still downgrades via the accusation gate."""
    _queue(mock_openai, [_verdict("NOT_SUPPORTED", "")])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": "Short paper about something else entirely.", "title": "Paper"}]
    result = vh.verify_single_claim(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "NOT_SUPPORTED"


def test_contradicted_requires_shown_quote(mock_openai):
    _queue(mock_openai, [_verdict("CONTRADICTED", "")])
    claim = {"claim_text": "Transformers [1] use self-attention.", "cited_refs": [1]}
    articles = [{"content": "Short paper about recurrence only.", "title": "Paper"}]
    result = vh.verify_single_claim(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "UNVERIFIABLE"
    assert "no evidence quote" in result.reasoning
