# Identical reference fetches within one batch

Two bibliography entries with the same complete parsed reference input can launch duplicate downloads before either result reaches the success cache. Coalesce those inputs within one resolve_and_fetch_all call. Each citation key retains its content and throttling outcome.

Grouping compares the complete parsed reference dictionary, not only the source-cache key. The same DOI with a different URL/title/raw fallback remains separate. This is intentionally conservative: equivalent-but-not-identical inputs may still download twice. Grouping uses local equality comparisons, with at most quadratic comparisons in the number of distinct inputs. No cross-request in-flight registry, new rate policy or retry allowance is added.

Only successful content is cached, as before. A shared empty/failure/exception is not cached and can retry on the next resolution. The existing cache-key policy is unchanged, including its separate pending PR13 fix. This change does not fix collisions between different inputs in that cache.

Stacked source base: unmerged upstream PR14 head f7dcce2c6a3ab44a152d7e45ece8f6ee7a3e1131. It does not include PR13. This is a fork-only draft on Harsh23Kashyap/HallucinationNerd targeting fix/nonfinite-retry-after, stacked on upstream PR14. Nothing is merged and this draft makes no upstream change.

Offline checks:
-16 new cases: baseline15fail/1pass, fixed16pass; ten repeats160checks pass.
-Broader nonbenchmark suite: baseline151pass/15newfailures/1benchmarkdeselected, fixed166pass/same1deselected.
-Ordinary/patient policy, arXiv/DOI/PMID/URL, throttle fan-out, exception/negative-cache avoidance, next-request retry, successful cache reuse, missing citations, author-year aliases and distinct fallback inputs.

External sockets blocked, no source/provider/model/scorer calls or paid runs. This reduces duplicate work for identical input, not proof of citation truth, retrieval quality, live-source coverage or billing savings. No merge or deployment.
