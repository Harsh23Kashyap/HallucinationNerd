# Scope cached source content to fetch inputs

`fetch_source_content` tries arXiv, DOI, direct URL, PubMed, title search and raw-text search. A primary identifier can fail while a fallback succeeds. Caching that fallback content by only the primary identifier lets a later reference with a different fallback receive the first reference's content without fetching its own source.

The resolution cache now combines the existing identifier key with an ordered tuple of the six fetch-selection fields: `arxiv_id`, `doi`, `url`, `pmid`, `title`, `raw`. Dictionary insertion order and citation numbering do not matter. Missing and empty fields have the same scope. The public `_make_cache_key` helper's existing policy stays unchanged.

## Trade-off and limits

This is conservative: a metadata variant no longer shares cached content solely because its primary identifier matches, even if that primary lookup would have succeeded. That can reduce cache hits and cause another permitted fetch attempt. Retry limits, rate policy, patient mode, transient-failure handling and per-batch coalescing are unchanged. This is not a claim of fewer requests or lower bills. No paid source, model or live retrieval was called for this work.

It does not track which backend produced each cached string, normalize all equivalent metadata, bound the cache's memory or entry count, add cross-request in-flight coalescing, or establish citation truth.

## Branch and overlap

The proposed fork-only draft is based on `7adac98e1065f5ad268a764ba4820812318bc9e9`, the exact head of fork PR3 (coalescing), which includes the PR14 retry fix but not PR13. Target: `feature/coalesce-reference-fetches` in the fork. It makes no upstream or merge change.

There is overlap in outcome with upstream PR13's direct-URL cache-key fix: full-input scoping also distinguishes direct URLs. This draft does not include or alter PR13's helper change. The additional reproduced case concerns references that share a primary canonical identifier but succeed through different fallbacks.

## Offline evidence

- 20 new tests: baseline 15 failed / 5 passed; fixed 20 passed.
- 48 new and existing cache/coalescing/key/TTL tests pass.
- Full nonbenchmark offline suite: fixed 186 passed / 1 benchmark deselected; exact-base comparison 171 passed / 15 new failures / same 1 benchmark deselected.
- Three tests execute the real `fetch_source_content` fallback path with all retrieval functions stubbed: a failed primary lookup uses URL A on one resolution, then URL B on the next. Each receives its own fallback rather than cached A.
- Cases also cover changed URL/title/raw/other fetch inputs, each variant reusing its own later cache entry, identical inputs under different citation numbers and dictionary order, plus uncached transient failures and throttling outcomes.

No merge, deployment, live-source quality, billing or scientific-correctness claim follows from these tests.
