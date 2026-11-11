# Direct URL identity with complete-input cache policy (option A)

`_make_cache_key` now considers the trimmed direct URL after arXiv/DOI and before
PMID/title, matching fetch order. URL path case and query parameters remain
significant. This incorporates the direct-URL identity part of upstream PR13
without its broader same-URL cache-reuse promise.

The actual cache continues to use `_fetch_cache_key`, which includes all six
fetch-selection fields: arxiv_id, doi, url, pmid, title and raw. A title/raw
change therefore causes another permitted fetch even if the URL is unchanged.
If a URL fails and title/raw fallback supplies text, the next reference cannot
inherit that text from a different fallback input. Identical fetch inputs still
reuse successes after citation renumbering or dictionary reordering.

The helper's same-URL identity and the actual cache's full-input scope are
deliberately different. Upstream PR13's expectation that changed display titles
always take just one fetch is not retained. The corresponding integration test
expects two fetches under option A, as an explicit policy choice, not a claim
that the upstream implementation met this policy. This can reduce cache hits
and add requests/wait time. No request-count or cost-saving claim.

## Branch and validation

Fork-only draft stacked on `fix/source-cache-concurrency`, exact head
0c5a66387fd82eb95f468398aa301966ddedc099 (fork PR5). It inherits fork PR4's
complete-fetch-input scope and PR3's per-batch coalescing. It does not modify
upstream branches or silently replace the existing drafts.

- 17 direct-URL/policy regression cases: exact stack base 7 failed / 10 passed;
  candidate 17 passed. Three added integration cases cover identical renumbered
  inputs, failed-URL/changed-title fallback, and changed raw input.
- 69 combined cache/key/TTL/concurrency cases pass.
- Broader offline suite: 223 passed / 1 benchmark deselected.

All retrieval is stubbed and external sockets blocked. No live source/model,
paid run, merge or deployment. This is cache policy/identity, not URL egress
protection, source authenticity or citation truth. Cache size and cross-request
in-flight coalescing remain outside scope.
