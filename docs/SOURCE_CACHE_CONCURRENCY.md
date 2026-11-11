# Source-cache concurrency

The process-local cache is shared by resolver calls. Expiry scanned the live
dictionary while other calls could insert successful fetches or remove expired
entries. A concurrent size change could raise RuntimeError before fetching,
interrupting that resolution.

A small lock now covers expiry scanning/removal, cache lookup and successful
cache writes. No lock is held while submitting or awaiting fetches, parsing
references, or running network/model work. Independent requests may still fetch
the same source concurrently. This is not cross-request single-flight, a global
request limiter, a resource bound, or a distributed cache.

## Offline evidence

Twenty added cases exercise controlled real-thread interleavings and unchanged
success/failure/throttle policy. The reader's dict iterator is paused to allow a
second real resolver call to compete. Four cases require two concurrent fetches
to reach a barrier, checking that cache locking does not serialize fetch waits.
The unmodified PR4 base fails 11 of these cases and passes nine; the fix passes
all 20. The equal-size mixed mutation control need not fail on the base.

An additional ordinary-dict stress run used 12 caller threads with five resolver
calls each, stubbed resolver/fetch boundaries and blocked sockets. At zero,
1,000 and 10,000 seeded entries, all 60 calls passed on both base and fix. At
100,000 entries, the main source snapshot and PR4 base reproduced dict-size
errors, while the fixed run completed 60/60. Interleavings and failure counts
are timing-dependent, not a claimed production frequency or a latency/memory
benchmark. No deployment incident has been observed.

Cache TTL, successful-content caching, failure retry policy, keys, fallback
selection, per-batch coalescing, patient attempts and fetch concurrency are
unchanged. The existing cache remains unbounded and uses wall-clock TTL. It
does not protect unrelated shared state. No external requests, model calls,
paid runs, merge or deployment were used to qualify it.
