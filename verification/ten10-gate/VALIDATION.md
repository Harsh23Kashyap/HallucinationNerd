# Prompt-fix validation (characterization-claim grounding)

## Change under test
verify_hallucinations.py: the characterization-claim rule was moved into the
PARTIALLY_SUPPORTED and NOT_SUPPORTED verdict definitions (explicit statement
of a characterization is not required; clear instances of the characterized
kind count as support; breadth mismatch caps at PARTIAL; NOT_SUPPORTED only
when no instances of the kind exist). Case-agnostic wording; no case-specific
content.

## Method
Two independent full 20-case re-runs (samples s1, s2) of the fixture in
pack.json at the prompt change, single-claim mode, gpt-4o, n_votes=1,
gate ON, full_source_recheck. Compared against the pre-change run at 3ee1c89
(results_3ee1c89.json, which used the fixture after the marker repair -
see "Fixture versions" below).

## Results
- 2106.13626 (the case that drew NOT_SUPPORTED pre-change): PARTIALLY_SUPPORTED
  in both samples; reasoning cites the theory-vs-experiment instances.
- Zero NOT_SUPPORTED/CONTRADICTED verdicts in either sample (zero accusations).
- No over-crediting: both coverage-limited abstains (2601.18824, 2604.17611)
  remained abstains in both samples; the comparative-claim cases did not
  upgrade on topical grounds.
- Reachability cost: 2607.04333, 2105.07053, 2103.00121 moved from
  SUPPORTED/PARTIAL to UNVERIFIABLE in both samples (grounding-check or
  coverage-gated abstains). Two further cases (2603.14304, 2604.17270) were
  unstable across samples, consistent with n_votes=1 draw noise.

## Caveats
- Two n_votes=1 samples bound but do not eliminate stochastic variation;
  quotation-grounding abstains in particular vary by draw.
- Pre-change vs post-change differences on single samples conflate the prompt
  change with draw noise; only effects stable across both samples are noted.

## Fixture versions
- v1: 17 verbatim clauses from the initial run record + 3 re-derived clauses
  without citation markers (repair commit 07e3b8a records this state).
- v2 (current pack.json): the 3 re-derived clauses carry the normalized [1]
  citation marker of the initial run's convention. results_3ee1c89.json
  reflects v2 inputs.

## Boundary controls (specific-claim generalization boundary)
Added a sentence to the PARTIALLY_SUPPORTED definition: the instance-based
reading applies only to characterization claims; a specific assertion (a
quantity, comparison, causal claim, or universal claim) is never supported
by related instances alone. Six controls, 2 samples each, at that state
(results_controls_boundary.json), plus one full 20-case sample
(validation_boundary_s1.json): zero accusations; Case A stayed
PARTIALLY_SUPPORTED; only the two previously unstable cases moved.

- N1 (quantity contradicted, vs 1301.1745v2): NOT_SUPPORTED x2. Holds.
- N2 (comparative universal, vs 2201.02177v1): NOT_SUPPORTED x2. Holds.
- N3 (causal, vs 2202.05607v2): PARTIALLY_SUPPORTED x2. Control mis-designed:
  the source genuinely says standard decision transformers collapse when used
  naively for online data; the claim is a borderline spin, so PARTIAL is
  defensible. Not counted as a defect.
- N3b (direction contradicted: "online finetuning harms decision
  transformers", vs 2202.05607v2, which shows finetuning helps):
  PARTIALLY_SUPPORTED x2 at the current prompt, and also x2 at both prior
  prompt states (3ee1c89 and the original acd226b). Over-credit pre-exists
  every tested prompt state: the judge leans on a genuine but
  direction-irrelevant passage ("standard DT collapses when used naively")
  and misreads direction. This is a judge-level directional reliability
  limit, prompt-resistant at this level; it is not introduced by the prompt
  changes recorded here and is not fixed by them.
- N4 (unsupported universal, vs 2102.07419v2): UNVERIFIABLE x2 (provisional
  negative, coverage-gated). Conservative abstain; no false pass.
- P1 (specific supported, vs 2102.07419v2): SUPPORTED x2. Positives survive.

Consequence for gate use: the verifier can be run without false accusations
on this evidence, but direction-of-effect claims carry a documented
over-credit risk (N3b type); aggregate hallucination rates from a study run
would understate direction-reversed errors unless such cases are adjudicated
by a human.
