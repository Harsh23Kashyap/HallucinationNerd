"""Tests for the arxiv_extractor citation-mapping hardening.

Covers: multi-citation pairing (every ref in [N, M]), citation ranges,
page header/footer stripping, heading-anchored section detection,
abbreviation-safe clause boundaries, alphanumeric labels, detached
bibliography labels, and mixed-format papers.
"""
import fitz
import pytest

import arxiv_extractor as ax


def _make_pdf(tmp_path, pages):
    """Build a real PDF from a list of page texts."""
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 550, 780), text, fontsize=10)
    path = tmp_path / "paper.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


NUMBERED_BIB = """
References
[1] Vaswani et al. Attention is all you need. arXiv:1706.03762
[5] Devlin et al. BERT: pre-training of deep bidirectional transformers. arXiv:1810.04805
[7] Brown et al. Language models are few-shot learners. arXiv:2005.14165
"""


def test_multi_cite_pairs_every_ref():
    section = "Self-attention models dominate NLP [5, 7] and keep improving."
    pairs = ax._extract_numbered(section, NUMBERED_BIB, "numbered_bracket")
    nums = {p["cite_num"] for p in pairs}
    assert nums == {5, 7}
    for p in pairs:
        assert "dominate NLP" in p["clause"]


def test_citation_range_expands():
    section = "Several architectures changed the field [1, 5-7] in recent years."
    pairs = ax._extract_numbered(section, NUMBERED_BIB, "numbered_bracket")
    assert {p["cite_num"] for p in pairs} == {1, 5, 7} or {p["cite_num"] for p in pairs} == {1, 5, 7}


def test_best_clause_wins_per_ref():
    section = ("Ok [5]. " + "x" * 25 + " [5]. "
               "BERT pre-trains deep bidirectional transformers with masked language modeling "
               "objectives and fine-tunes downstream [5].")
    pairs = ax._extract_numbered(section, NUMBERED_BIB, "numbered_bracket")
    assert len(pairs) == 1
    assert "masked language modeling" in pairs[0]["clause"]


def test_detached_numeric_bib_labels():
    bib = "\nReferences\n12.\nVaswani et al. Attention is all you need. arXiv:1706.03762\n13.\nDevlin et al. BERT. arXiv:1810.04805\n"
    section = "Transformers reshaped sequence modeling entirely [12] and beyond."
    pairs = ax._extract_numbered(section, bib, "numbered_bracket")
    assert [p["cite_num"] for p in pairs] == [12]
    assert pairs[0]["arxiv_id"] == "1706.03762"


def test_alpha_labels():
    bib = "\nReferences\n[Devlin18] Devlin et al. BERT. arXiv:1810.04805\n[Vaswani+17] Vaswani et al. Attention is all you need. arXiv:1706.03762\n"
    section = ("Pre-trained encoders improved every benchmark they touched [Devlin18] in NLP. "
               "Attention-only models scaled further [Vaswani+17] afterward.")
    pairs = ax._extract_alpha(section, bib)
    keys = {p["cite_key"] for p in pairs}
    assert keys == {"Devlin18", "Vaswani+17"}


def test_abbreviation_does_not_truncate_clause():
    text = "As Smith et al. showed, pre-training transfers well across tasks [5] overall."
    clause = ax._get_clause_before(text, text.index("[5]"), r"\[[\d,\s]+\]")
    assert "et al." in clause
    assert "pre-training transfers well" in clause


def test_strip_page_artifacts():
    pages = [
        "Related Work\n3\nReal content line one.\n14",
        "Related Work\n3\nReal content line two.\n15",
        "Related Work\n3\nReal content line three.\n16",
    ]
    cleaned = ax._strip_page_artifacts(pages)
    assert "Real content line one." in cleaned
    assert "Related Work\n3" not in cleaned
    assert "\n14" not in cleaned and "\n15" not in cleaned


def test_heading_anchored_section_detection():
    text = ("We discuss how our approach differs from related work in the appendix.\n"
            "Some intro paragraph.\n"
            "2. Related Work\n"
            "Transformers reshaped NLP [1] completely.\n"
            "3. Method\n"
            "Our method section.\n"
            "References\n[1] Vaswani et al. arXiv:1706.03762\n")
    start = ax._find_section_start(text, "related work")
    assert text[start:start + 40].lstrip().startswith("2. Related Work")


def test_pdf_end_to_end_with_headers_and_mixed_formats(tmp_path):
    header = "Conference Draft 2026"
    body_p1 = (
        "1. Introduction\n"
        "Attention models are everywhere.\n"
        "2. Related Work\n"
        "Self-attention models dominate NLP [5, 7] and keep improving. "
        "Pre-training also transfers broadly (Smith et al., 2020) in practice.\n"
        "3. Method\n"
        "Our method.\n"
    )
    refs = (
        "References\n"
        "[5] Devlin et al. BERT: pre-training of deep bidirectional transformers. arXiv:1810.04805\n"
        "[7] Brown et al. Language models are few-shot learners. arXiv:2005.14165\n"
        "Smith et al. 2020. Broad transfer of pre-trained representations. arXiv:2010.11977\n"
    )
    pages = [
        f"{header}\n{body_p1}\n1",
        f"{header}\n{refs}\n2",
        f"{header}\nAppendix content.\n3",
    ]
    pdf = _make_pdf(tmp_path, pages)
    pairs = ax.extract_citations_from_pdf(pdf)
    by_id = {p["arxiv_id"]: p for p in pairs}
    # numbered pair from the mixed-format paper
    assert "1810.04805" in by_id
    assert "2005.14165" in by_id
    # author-year pair from the same section (mixed formats both extracted)
    assert "2010.11977" in by_id
    # no pair may carry running-head text in its clause
    for p in pairs:
        assert header not in p["clause"]


def test_pdf_section_not_confused_by_prose_mention(tmp_path):
    body = (
        "1. Intro\n"
        "We survey related work informally here without extracting it [9].\n"
        "2. Related Work\n"
        "Self-attention models dominate NLP [5] and keep improving.\n"
        "3. Method\n"
        "Method text.\n"
        "References\n"
        "[5] Devlin et al. BERT. arXiv:1810.04805\n"
        "[9] Ghost et al. Not really cited work. arXiv:1901.00001\n"
    )
    pdf = _make_pdf(tmp_path, [body])
    pairs = ax.extract_citations_from_pdf(pdf)
    nums = {p.get("cite_num") for p in pairs}
    assert 5 in nums
    assert 9 not in nums  # citation from the prose mention, not the section
