# Portable JSON review bundle

Package an existing `/verify` JSON response for offline review:

```sh
python tools/review_bundle.py build --report report.json --output review-bundle.json
python tools/review_bundle.py check --bundle review-bundle.json
python tools/review_bundle.py check --bundle review-bundle.json --original-report report.json
```

Python 3.10+ and the standard library are sufficient. No HTTP/model/retrieval
calls, server startup or verdict changes. This complements the HTML export with
a machine-readable round-trip format. There is no frontend button, new API
route or automatic saving. Obtain an intended response through the existing
API workflow first; this tool does not run verification for you.

## Retained data and supported shape

The payload keeps the complete parsed JSON report, including unknown fields,
claims, verdicts, confidence, evidence, reasoning and source metadata. Derived
linkage lists document-local labels used by each claim, missing metadata,
uncited claims and unused source entries. Duplicate references in one claim
count once in linkage; the original array is unchanged. Numeric `1` and string
`1` share a source-map key. Author-year labels stay exact strings. No identity
is inferred across documents and missing metadata is never invented.

Reports require a `claims` array, `source_map` object and `summary.total_claims`
integer equal to the array length. Each claim needs nonblank `claim`/`verdict`,
finite confidence from 0 through 1, and `cited_refs` as positive integers or
nonblank trimmed strings. Source title/URL/DOI, when present, must be strings.
Empty reports with count zero are valid. Other fields and unknown verdicts are
retained, not semantically checked or recategorized. Summary reliability and
precision arithmetic are not validated. Other endpoint shapes are unsupported.

Duplicate JSON keys and nonfinite numbers are rejected, even in unused fields.
Report text is data, never interpreted as code, fetched as a URL or used as a
filesystem path. Paths come only from command-line arguments.

## Integrity, not provenance

The manifest records original input-byte SHA-256, canonical parsed-report
SHA-256 and canonical payload SHA-256. Canonical means Python sorted-key,
compact JSON, UTF-8, no ASCII escaping/nonfinite numbers. This is this tool's
format, not RFC 8785 or cross-language canonicalization. Key order/whitespace
do not change canonical hashes; whitespace changes the original-byte hash.

`check` recomputes hashes and linkage. Without `--original-report`, the input
byte hash is a retained assertion, not independently checked. With that option,
the bytes and parsed original must match. Status explicitly says whether
original bytes were checked and always says authenticity/retrieval unverified.

Anyone can edit a report and recompute hashes. A self-consistent bundle is not
signed, trusted or evidence of its author. Source-map URLs are copied metadata,
not proof of an actual retrieval route or fetched content. No execution trace,
source bytes, model/prompt identity, timestamp, scientific validity or billing
provenance is created. Passing a check never means a claim is true.

## Privacy and file behavior

This is NOT a redacted manifest. It contains private document text, evidence
and source details. Review the audience before sharing. Nothing is uploaded or
automatically shared. Checks print only status/counts; errors omit contents and
paths. Inputs load into memory without a size quota. Use one writer per output.

Build refuses an output resolving to the source. Another existing output is
replaced atomically only after validation/serialization. Invalid input leaves
old output unchanged, so check exit status rather than file existence. Atomic
rename is not durability or a multi-writer guarantee. Output directory must
exist. Exit 0 means build/check succeeded under this contract; exit 2 means
invalid input, integrity mismatch or I/O failure.

## Tests

`python -m pytest -q tests/test_review_bundle.py`

Synthetic tests cover lossless parsed-data round-trip, label linkage, missing
metadata, uncited/empty reports, tampering, malformed input, duplicate keys,
nonfinite values, CLI file preservation, and rehashing without authenticity.
