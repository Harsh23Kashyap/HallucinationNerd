"""Offline tests for author-year (unnumbered) citation support."""
import importlib.util, os

BASE = os.path.join(os.path.dirname(__file__), "..", "web")

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BASE, path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

cr = _load("citation_resolver", "citation_resolver.py")
pe = _load("pdf_claim_extractor", "pdf_claim_extractor.py")

ACL_DOC = """We use BERT (Devlin et al., 2019) as the encoder. Attention is all you need
(Vaswani et al., 2017) introduced the transformer. Peters et al. (2018a) showed
contextual embeddings help.

References
Jacob Devlin, Ming-Wei Chang, Kenton Lee, and Kristina Toutanova. 2019. BERT:
Pre-training of deep bidirectional transformers for language understanding.
In Proceedings of NAACL.
Ashish Vaswani, Noam Shazeer, Niki Parmar, and Jakob Uszkoreit. 2017.
Attention is all you need. In Advances in neural information processing
systems.
Matthew E. Peters, Mark Neumann, Mohit Iyyer, and Matt Gardner. 2018a. Deep
contextualized word representations. arXiv preprint arXiv:1802.05365.
"""

NEURIPS_DOC = """Pre-training methods learn from raw text (Howard and Ruder, 2018; Devlin et
al., 2019). Skip-thought vectors (Kiros et al., 2015) embed sentences.

References
Devlin, J., Chang, M.-W., Lee, K., and Toutanova, K. Bert: Pre-training of
deep bidirectional transformers for language understanding. arXiv preprint
arXiv:1810.04805, 2019.
Howard, J. and Ruder, S. Universal language model fine-tuning for text
classification. arXiv preprint arXiv:1801.06146, 2018.
Kiros, R., Zhu, Y., Salakhutdinov, R. R., Zemel, R., Urtasun, R., Torralba,
A., and Fidler, S. Skip-thought vectors. In NIPS, 2015.
"""

NUMBERED_DOC = """Transformers dominate [1]. Layer norm helps [2].

References
[1] Ashish Vaswani et al. Attention is all you need. In NIPS, 2017.
[2] Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. Layer
normalization. arXiv preprint arXiv:1607.06450, 2016.
"""

JUNK_NUMBERED_DOC = """The model reaches 0.98 accuracy (see Table 2).
2016. was a good year for baselines.

References
Vaswani, A., Shazeer, N., Parmar, N., and Uszkoreit, J. Attention is all you
need. In NIPS, 2017.
"""


def test_author_year_refs_parsed_acl_style():
    refs = cr.resolve_references(ACL_DOC)
    assert "devlin2019" in refs
    assert "vaswani2017" in refs
    assert "peters2018a" in refs
    assert refs["devlin2019"]["title"].startswith("BERT")


def test_author_year_refs_parsed_surname_first_style():
    refs = cr.resolve_references(NEURIPS_DOC)
    assert "devlin2019" in refs
    assert "howard2018" in refs
    assert "kiros2015" in refs
    assert "skip-thought" in refs["kiros2015"]["title"].lower()


def test_author_year_claim_extraction():
    claims = pe.extract_cited_claims_from_text(ACL_DOC)
    keys = {r for c in claims for r in c["cited_refs"]}
    assert "devlin2019" in keys
    assert "vaswani2017" in keys
    assert "peters2018a" in keys


def test_numbered_parse_survives():
    refs = cr.resolve_references(NUMBERED_DOC)
    assert "1" in refs and "2" in refs
    assert refs["2"]["arxiv_id"] == "1607.06450"
    assert "vaswani" in refs["1"]["raw"].lower()


def test_junk_numbered_parse_rejected():
    # Spurious "\n2016." hits must not become reference keys.
    refs = cr.resolve_references(JUNK_NUMBERED_DOC)
    assert "2016" not in refs
    assert "vaswani2017" in refs


def test_fuzzy_year_suffix_fallback():
    refs = cr.resolve_references(ACL_DOC)
    assert "peters2018a" in refs
