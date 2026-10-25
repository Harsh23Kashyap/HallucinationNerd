"""Regression tests for the two false-pass failures found in the 10+10 review.

1. RankNet wrong-source: an author-year paper citing "Burges et al. [2005]"
   was parsed as numbered citation [2005]; a wrapped bibliography line
   ("2005.") mimicked a numeric label and mapped it to an unrelated entry
   (arXiv:2505.23749). The extractor must not invent that mapping.
2. Rubric: topical relatedness must not force PARTIALLY_SUPPORTED when the
   claim's core subject/assertion is absent, and methodology inconsistencies
   count as CONTRADICTED.
"""
import arxiv_extractor as ax
import verify_hallucinations as vh

BIB = """
References
Chris Burges, Tal Shaked, Erin Renshaw, Ari Lazier, Matt
Deeds, Nicole Hamilton, and Greg Hullender. Learning to
rank using gradient descent. In Proceedings of the 22nd
international conference on Machine learning, pages 89-96,
2005.
Luise Ge, Daniel Halpern, Evi Micha, Ariel D Procaccia. Axioms
for ai alignment from human feedback. Advances in Neural
Information Processing Systems, 37:80439-80465,
2024.
"""


def test_wrapped_year_not_a_bib_label():
    entries = ax._parse_numbered_bibliography(BIB)
    assert 2005 not in entries
    assert 2024 not in entries


def test_narrative_bracket_year_is_author_year_not_numbered():
    text = ("These methods build on classical pairwise ranking losses such as "
            "RankNet, which optimize smooth surrogates Burges et al. [2005]. "
            "Other work [3] continues.")
    formats = dict(ax.detect_citation_format(text))
    assert "author_year_bracket_narrative" in formats
    # [2005] must not count as a numbered citation; [3] still does
    assert formats.get("numbered_bracket", 0) == 1


def test_extract_numbered_ignores_narrative_year():
    section = ("These methods build on classical pairwise ranking losses such as "
               "RankNet, which optimize smooth surrogates of ranking quality "
               "using logistic link functions Burges et al. [2005].")
    pairs = ax._extract_numbered(section, BIB, "numbered_bracket")
    assert pairs == []


def test_rubric_has_no_topical_connection_mandate():
    p = vh.CITATION_VERIFICATION_PROMPT
    assert "ANY topical connection" not in p
    assert "Topical relatedness is not support" in p
    assert "baseline data" in p


def test_rubric_requires_actual_method_use_not_mentions():
    p = vh.CITATION_VERIFICATION_PROMPT
    assert "what I actually did" in p
    assert "only as future work" in p
    assert "only baseline data were used" in p
