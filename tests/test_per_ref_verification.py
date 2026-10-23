"""Tests for per-ref verification (C1 — the headline fix).

The website's 55-62% drop was caused by a single LLM call that concatenated
all cited sources into one source block. For a 5-ref claim, the LLM saw
~1/5 of the relevant content and returned PARTIALLY_SUPPORTED. Per-ref
verification issues one LLM call per ref, then aggregates.
"""
import json
import pytest


def test_per_ref_aggregates_supported(mock_openai):
    """If any ref returns SUPPORTED, the claim is SUPPORTED (best-ref-wins)."""
    from verify_hallucinations import verify_claim_per_ref
    # ref [1] is NOT_SUPPORTED, ref [2] is SUPPORTED -> claim should be SUPPORTED
    def verdicts():
        for v in ["NOT_SUPPORTED", "SUPPORTED"]:
            yield json.dumps({
                "verdict": v,
                "confidence": 0.9,
                "evidence_quote": "test",
                "evidence_start_phrase": "test",
                "reasoning": f"forced {v}",
            })
    g = verdicts()
    def fake_create(*args, **kwargs):
        mock_openai.next_json = next(g)
        return mock_openai._FakeResponse__dict__ if False else None  # not used; next_json consumed in create()
    # Easier: set next_json right before each call by using a queue via mock's next_json
    # We'll set two distinct next_json values and let the loop pick them in order
    # The mock consumes next_json, so we need a different approach: use next_verdict
    from verify_hallucinations import verify_claim_per_ref
    # Use a list of next_verdict values; mock consumes one per call
    # We have to monkey-patch the mock to not consume (so we can pre-load)
    queue = iter([
        {"verdict": "NOT_SUPPORTED", "confidence": 0.9, "evidence_quote": "", "reasoning": "no"},
        {"verdict": "SUPPORTED", "confidence": 0.95, "evidence_quote": "Adapter paper content", "reasoning": "yes"},
    ])
    real_create = mock_openai.create
    def queue_create(*args, **kwargs):
        mock_openai.next_verdict = next(queue)
        return real_create(*args, **kwargs)
    mock_openai.create = queue_create

    claim = {
        "claim_text": "Transformers [1] use attention; adapters [2] are efficient.",
        "cited_refs": [1, 2],
    }
    articles = [
        {"content": "Transformer paper content..."},
        {"content": "Adapter paper content..."},
    ]
    result = verify_claim_per_ref(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "SUPPORTED"
    assert result.cited_refs == [1, 2]
    assert result.evidence_reference == "2"  # strongest ref
    assert len(result.per_ref_verdicts) == 2
    assert result.per_ref_verdicts[0]["verdict"] == "NOT_SUPPORTED"
    assert result.per_ref_verdicts[1]["verdict"] == "SUPPORTED"


def test_per_ref_all_unsupported(mock_openai):
    """If all refs return NOT_SUPPORTED, the claim is NOT_SUPPORTED."""
    from verify_hallucinations import verify_claim_per_ref
    queue = iter([
        {"verdict": "NOT_SUPPORTED", "confidence": 0.9, "evidence_quote": "", "reasoning": "n1"},
        {"verdict": "NOT_SUPPORTED", "confidence": 0.9, "evidence_quote": "", "reasoning": "n2"},
    ])
    real_create = mock_openai.create
    def queue_create(*args, **kwargs):
        mock_openai.next_verdict = next(queue)
        return real_create(*args, **kwargs)
    mock_openai.create = queue_create

    claim = {
        "claim_text": "Foo [1] and bar [2] do X.",
        "cited_refs": [1, 2],
    }
    articles = [
        {"content": "Substantial content for the first source paper on transformers."},
        {"content": "Substantial content for the second source paper on adapters."},
    ]
    result = verify_claim_per_ref(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "NOT_SUPPORTED"
    assert len(result.per_ref_verdicts) == 2


def test_per_ref_missing_content(mock_openai):
    """A ref with no content gets UNVERIFIABLE, others still evaluated."""
    from verify_hallucinations import verify_claim_per_ref
    queue = iter([
        {"verdict": "SUPPORTED", "confidence": 0.9, "evidence_quote": "substantial content", "reasoning": "y"},
    ])
    real_create = mock_openai.create
    def queue_create(*args, **kwargs):
        mock_openai.next_verdict = next(queue)
        return real_create(*args, **kwargs)
    mock_openai.create = queue_create

    # ref 1 has empty content; ref 2 has substantial content (>= 20 chars)
    claim = {"claim_text": "X [1] and Y [2].", "cited_refs": [1, 2]}
    articles = [{"content": ""}, {"content": "This is the substantial content of the second source paper."}]
    result = verify_claim_per_ref(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "SUPPORTED"
    assert result.per_ref_verdicts[0]["verdict"] == "UNVERIFIABLE"
    assert result.per_ref_verdicts[0]["error"] == "no_content"


def test_per_ref_out_of_range(mock_openai):
    """A cited ref number not in articles list gets UNVERIFIABLE."""
    from verify_hallucinations import verify_claim_per_ref
    queue = iter([
        {"verdict": "SUPPORTED", "confidence": 0.9, "evidence_quote": "substantial content", "reasoning": "y"},
    ])
    real_create = mock_openai.create
    def queue_create(*args, **kwargs):
        mock_openai.next_verdict = next(queue)
        return real_create(*args, **kwargs)
    mock_openai.create = queue_create

    claim = {"claim_text": "X [5] and Y [1].", "cited_refs": [5, 1]}  # 5 out of range
    articles = [{"content": "This is the substantial content of the only article we have."}]
    result = verify_claim_per_ref(claim, articles, question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "SUPPORTED"
    assert result.per_ref_verdicts[0]["verdict"] == "UNVERIFIABLE"
    assert result.per_ref_verdicts[0]["error"] == "missing"


def test_per_ref_no_citations():
    """Uncited claim returns UNVERIFIABLE without LLM call."""
    from verify_hallucinations import verify_claim_per_ref
    claim = {"claim_text": "Just a claim.", "cited_refs": []}
    result = verify_claim_per_ref(claim, [], question="Q", question_id="Q1", claim_idx=1)
    assert result.verdict == "UNVERIFIABLE"
    assert "No citation provided" in result.reasoning


def test_per_ref_no_citations_with_search_backup(mock_openai):
    """Uncited claim with search_backup triggers PubMed search."""
    from verify_hallucinations import verify_claim_per_ref
    mock_openai.next_json = json.dumps({
        "verdict": "SUPPORTED",
        "confidence": 0.8,
        "evidence_quote": "backup",
        "reasoning": "found backup",
    })
    # search_fn is a mock that returns a "found" result
    class _Found:
        def get(self, k, default=""):
            return {"title": "Backup", "content": "Backup content", "url": "http://x"}.get(k, default)
    class _SearchFn:
        def __call__(self, q):
            return [_Found()]
    result = verify_claim_per_ref(
        {"claim_text": "Uncited.", "cited_refs": []},
        [], question="Q", question_id="Q1", claim_idx=1,
        search_backup=True, search_fn=_SearchFn(),
    )
    assert result.verdict == "BACKUP_FOUND"
