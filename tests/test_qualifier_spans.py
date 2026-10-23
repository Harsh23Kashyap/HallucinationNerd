"""Tests for qualifier-aware span selection.

Failure mode (SCOPE-PD, Oct 23 live run): the excerpt carried the matched
anchor sentence ("each participant undergoes repeated evaluations ...") but
not the restricting sentence beside it ("only the baseline (i.e., first-visit)
data were used"). Restriction/contrast sentences score low on claim keyword
overlap, so top-k chunk selection misses them even next to a matched anchor.
The qualifier pass includes marker sentences near selected chunks.
"""
import verify_hallucinations as vh


def _scope_pd_like_source():
    """A >4000-char source shaped like the SCOPE-PD case: the anchor sentence
    and its restricting sentence sit together mid-document, wrapped in filler
    that shares no claim keywords."""
    head = ("General background paragraph about cohort studies and enrollment. "
            ) * 25
    anchor = ("In this dataset, each participant undergoes repeated evaluations "
              "across multiple study visits, giving longitudinal measurements "
              "for every enrolled participant. ")
    restrict = ("However, only the baseline (i.e., first-visit) data were used "
                "for the present analysis. ")
    tail = ("Unrelated discussion of prior art, hardware details, and appendix "
            "pointers. ") * 60
    return head + anchor + restrict + tail


CLAIM = ("The SCOPE-PD dataset uses repeated assessments for each participant "
         "rather than only baseline snapshots.")


def test_restriction_sentence_near_anchor_is_included():
    excerpt, covered = vh._find_relevant_spans_with_coverage(CLAIM, _scope_pd_like_source())
    assert "repeated evaluations" in excerpt          # anchor still selected
    assert "only the baseline (i.e., first-visit) data were used" in excerpt


def test_abbreviation_does_not_fragment_qualifier():
    excerpt, _ = vh._find_relevant_spans_with_coverage(CLAIM, _scope_pd_like_source())
    # the i.e. parenthetical must survive intact, not split mid-sentence
    assert "(i.e., first-visit)" in excerpt


def test_marker_far_from_selection_not_included():
    src = _scope_pd_like_source() + (
        "Distant closing note: however, this unrelated remark sits far away. "
    ) + ("More unrelated filler about typesetting and margins. " * 40)
    excerpt, _ = vh._find_relevant_spans_with_coverage(CLAIM, src)
    assert "Distant closing note" not in excerpt


def test_excerpt_stays_within_budget():
    # marker-dense neighborhood must not blow the excerpt budget
    markers = ("However, this restriction matters here. " * 40)
    src = ("General background paragraph about cohort studies and enrollment. " * 25
           + "In this dataset, each participant undergoes repeated evaluations "
             "across multiple study visits. "
           + markers
           + "Unrelated discussion of prior art and hardware. " * 60)
    excerpt, _ = vh._find_relevant_spans_with_coverage(CLAIM, src)
    assert len(excerpt) <= 4000


def test_qualifier_budget_caps_additions():
    # 40 distinct marker sentences (~45 chars each = ~1800 chars) within the
    # neighborhood; only ~900 chars worth may be added
    markers = "".join(
        f"However, restriction number {i:02d} applies here. " for i in range(40)
    )
    src = ("General background paragraph about cohort studies and enrollment. " * 25
           + "In this dataset, each participant undergoes repeated evaluations "
             "across multiple study visits. "
           + markers
           + "Unrelated discussion of prior art and hardware. " * 60)
    excerpt, _ = vh._find_relevant_spans_with_coverage(CLAIM, src)
    present = sum(1 for i in range(40) if f"restriction number {i:02d}" in excerpt)
    assert 0 < present < 40  # some included, cap respected


def test_short_content_unchanged():
    short = "Short source text with only a baseline remark."
    excerpt, covered = vh._find_relevant_spans_with_coverage(CLAIM, short)
    assert excerpt == short
    assert covered == [(0, len(short))]


def test_legacy_wrapper_matches_coverage_variant():
    src = _scope_pd_like_source()
    assert vh._find_relevant_spans(CLAIM, src) == \
        vh._find_relevant_spans_with_coverage(CLAIM, src)[0]
