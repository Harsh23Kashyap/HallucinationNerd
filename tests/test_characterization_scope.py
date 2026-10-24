"""Characterization/scope claims must not draw flat NOT_SUPPORTED accusations.

Motivating failure (post-fix 10+10 rerun, 2106.13626 cites 1301.1745v2): the
claim "There is a scatter of theoretical and experimental data on the main
values of the electronic parameters of polytypes" characterizes a body of
evidence. The source's own Table 2 places calculated and experimental band
gaps side by side with visible disagreement (Ge 0.01/0.39 vs 0.74), and its
text says HSE accords well with experiment "except Ge". The judge returned
NOT_SUPPORTED on scope grounds - the claim speaks of polytypes in general,
the source covers one family and one property. Human adjudication (Harsh,
24 Oct 2026): the right verdict is PARTIALLY_SUPPORTED. The rubric now tells
the judge: representative instances of the characterized kind support at
least PARTIAL, and breadth mismatch caps at PARTIAL, never flat accusation,
when the covered instance is of the claimed kind.
"""
import verify_hallucinations as vh


CASE_A_CLAIM = ("There is a scatter of theoretical and experimental data on "
                "the main values of the electronic parameters of polytypes.")


def test_rubric_carries_characterization_rule():
    assert "CHARACTERIZATION CLAIMS" in vh.CITATION_VERIFICATION_PROMPT


def test_rubric_caps_breadth_mismatch_at_partial():
    prompt = vh.CITATION_VERIFICATION_PROMPT
    assert "breadth mismatch limits the verdict to PARTIALLY_SUPPORTED" in prompt
    assert "not NOT_SUPPORTED" in prompt


def test_rubric_requires_representative_evidence_for_accusation():
    assert ("Choose NOT_SUPPORTED only when your content contains no evidence "
            "of the kind the claim characterizes") in vh.CITATION_VERIFICATION_PROMPT


def test_case_a_claim_still_reaches_the_judge():
    # the fix steers the judge; it must not short-circuit real claims out
    assert not vh._is_non_assertion_fragment(CASE_A_CLAIM)


def test_characterization_rule_is_not_a_free_pass():
    # topical relatedness alone remains NOT_SUPPORTED per the existing rule;
    # both rules must coexist in the same prompt
    prompt = vh.CITATION_VERIFICATION_PROMPT
    assert "Topical relatedness is not support" in prompt
    i_related = prompt.index("Topical relatedness is not support")
    i_char = prompt.index("CHARACTERIZATION CLAIMS")
    assert i_related < i_char  # accusation discipline stated first
