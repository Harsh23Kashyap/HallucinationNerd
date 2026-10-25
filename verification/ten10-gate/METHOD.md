# Verifier 10+10 gate fixture and method

## Fixture
pack.json: 20 citation-verification cases, 10 citing papers from calendar 2026
(before Oct 15 2026) and 10 from calendar 2021 (same window), one arXiv-cited
assertion clause per citing paper, selected by seeded week sampling (seed 1010).
citing_version is v1 for every citing paper; source_version is the latest
version of the cited paper before the cutoff, resolved from the arXiv abs-page
submission history. All papers are public arXiv preprints.

## Clause provenance
17/20 clauses are verbatim from the initial run's recorded output
(clause_provenance=verbatim_from_initial_run). 3/20 (2609.14616, 2603.07451,
2110.04212) were re-derived from the immutable citing v1 PDF after the initial
run's local artifacts were lost (clause_provenance=re_derived_from_citing_pdf_labeled).
The re-derived clauses make this a labeled reconstruction, not a bit-exact
rerun of the initial pack; the difference is limited to those three clauses.

## Verifier configuration
Branch fix/verifier-reliability-citation-mapping at 3ee1c89; gate ON;
single-claim mode; judge model gpt-4o; n_votes=1; rate_limit_delay 0.3;
full_source_recheck enabled. Source PDFs fetched at the recorded version.

## Evaluation rule
Each case's verdict is judged on its own evidence. The gate criterion is the
absence of FALSE accusations (a NOT_SUPPORTED or CONTRADICTED verdict that the
human-adjudicated ground truth does not support). It is not a target count of
any verdict label, and known extractor limitations do not excuse over-crediting;
any over-credit observed in the re-run is reported as a finding.

## Outcome at 3ee1c89 (results_3ee1c89.json)
3 SUPPORTED, 6 PARTIALLY_SUPPORTED, 10 UNVERIFIABLE, 1 NOT_SUPPORTED.
The gate FAILED: one NOT_SUPPORTED verdict was issued on a case whose
evidence (a theory-vs-experiment comparison table in the cited source) the
judge's own reasoning describes, making the accusation false under the
evaluation rule above. The characterization-claims instruction added to the
judge prompt in 3ee1c89 was present in the active prompt but was not applied
by the judge in this n_votes=1 sample. Abstentions (UNVERIFIABLE) are not
accusations; their elevated count relative to the initial run is recorded as
a reachability observation, not a failure. Clause provenance: 17 verbatim
from the initial run, 3 re-derived and labeled; this is a labeled
reconstruction, not a bit-exact rerun.
