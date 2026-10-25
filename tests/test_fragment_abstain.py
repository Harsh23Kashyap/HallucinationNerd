"""Ablation/extraction fragments must abstain (UNVERIFIABLE), never accuse.

Motivating failure (post-fix 10+10 rerun, 2110.04710 cites 1712.00080): the
clause "2P+SS baseline ... • S1+SS [1]" is ablation shorthand whose citation
is a method-attribution pointer (SS = the cited Super SloMo paper). The
tightened rubric pushed the judge to NOT_SUPPORTED - a false accusation on a
non-assertion fragment. The correct behavior is abstention, as the earlier
review round ruled.
"""
import re
import verify_hallucinations as vh


FRAGMENT = ("2P+SS baseline with ours and in case the combination in P2P+SS makes "
            "the other slow down, we replace our Stage-I method with Pix2PixHD and "
            "keep our Stage-II (S2) method unchanged, which makes the comparison "
            "between S2 and SS a controlled experiment. \u2022 S1+SS [1].")


def test_detector_flags_ablation_bullet():
    assert vh._is_non_assertion_fragment(FRAGMENT)


def test_detector_flags_table_row():
    assert vh._is_non_assertion_fragment("\u2022 P2P+SS [3]")


def test_detector_ignores_real_claim_with_method_names():
    assert not vh._is_non_assertion_fragment(
        "BERT outperforms GPT-2 on the SQuAD 2.0 benchmark [1].")


def test_detector_ignores_plain_claim():
    assert not vh._is_non_assertion_fragment(
        "The bandgap calculated using HSE accords well with experimental values [2].")


def test_no_bullet_no_fragment():
    # method-heavy but no bullet/table marker -> still judged, not auto-abstained
    assert not vh._is_non_assertion_fragment(
        "S1 and S2 are combined as S1+S2 in the final model [4].")


def _claim():
    return {"claim_text": FRAGMENT, "cited_refs": [1]}


def _articles():
    return [{"title": "Super SloMo", "content": "Video interpolation. " * 400}]


def test_single_claim_fragment_abstains_without_judge(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(vh, "llm_call", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "{}")
    r = vh.verify_single_claim(_claim(), _articles(), "q", "qid", 0)
    assert r.verdict == "UNVERIFIABLE"
    assert "fragment" in r.reasoning.lower()
    assert calls["n"] == 0  # no judge call at all


def test_per_ref_fragment_abstains_without_judge(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(vh, "llm_call", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "{}")
    r = vh.verify_claim_per_ref(_claim(), _articles(), "q", "qid", 0)
    assert r.verdict == "UNVERIFIABLE"
    assert calls["n"] == 0


def test_rubric_instructs_fragment_abstention():
    assert "fragment" in vh.CITATION_VERIFICATION_PROMPT.lower()
    assert "method-attribution pointer" in vh.CITATION_VERIFICATION_PROMPT
