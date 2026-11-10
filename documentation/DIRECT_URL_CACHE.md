# Direct-URL source cache

References with the same title can point to different documents. The previous
cache key ignored their direct URLs, so resolving the second document could
return the first document's cached text without fetching its source.

When arXiv and DOI identifiers are absent, key the cache by the trimmed direct
URL before falling back to PMID or title, matching the source-fetch order.
Preserve URL path case and query parameters. This does not validate the URL,
prove the fetched text matches a citation, or change search/title matching.

Offline validation on main 86b4094:

```sh
python -B -m unittest discover -s tests -p test_direct_url_cache.py -v
python -B -m pytest -q -m 'not benchmark and not integration'
```

The 14 added tests give 9 failures/5 passes on unchanged main and 14 passes
with the fix, including actual resolver/cache calls with controlled fetches.
Twenty repeats pass 280 checks. With sockets blocked, the broader selection
gives 9 failures/138 passes/1 deselected on main and 147 passes/1 deselected
with the fix. The deselected benchmark is not a paid quality run. No live
source/provider/model calls, merge or deployment are included in this result.
