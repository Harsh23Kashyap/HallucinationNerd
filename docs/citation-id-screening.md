# Citation-ID screening, review implementation

An explicit `verification_mode=citation_existence` multipart field on `POST /verify`
selects arXiv metadata screening. Missing mode remains `claim_support`. Both UIs
keep full checking selected initially. Selecting the deeper-check button only
selects a mode, it does not submit another request.

## Scope

- Structured JSON with `retrieved_articles`, or numbered bibliography entries
  under an explicit References/Bibliography/Works Cited heading in PDF, HTML,
  text or Markdown. PDF extraction uses local text extraction, not OCR.
- Supported arXiv IDs only, including exact versions. Unsupported, ambiguous,
  missing, failed and absent metadata stay UNKNOWN. No negative existence proof.
- Per-row identifier status, separate NOT_CHECKED fields for bibliography identity
  and claim support, explicit coverage warnings and unknown-reason counts.
- No model, source PDF retrieval, title search, backup search, automatic retry or
  automatic escalation. Reference-file uploads and patient retries are rejected
  in this mode. Full-check behavior remains separate.
- Offline mode bypasses the resolver entirely. Private mode sends public IDs only.
- Fixed HTTPS metadata endpoint, redirects disabled, streamed 5 MB response cap,
  at most 100 unique IDs and 1000 source rows, 60-second metadata budget,
  per-I/O timeouts, 3.1-second process-wide request spacing and positive-only
  24-hour bounded in-memory cache. HTTP 429 stops the queue and applies a
  60-second process-wide cooldown. Partial successful results are retained.
- Disconnect signals stop queued work; in-flight blocking I/O finishes at its
  timeout before cleanup. The metadata budget is not a strict whole-upload SLA:
  upload/parsing time and at most one blocking I/O interval are additional.

## Qualification

`python -m pytest tests -q` runs repository tests. New regressions are in
`tests/test_citation_existence.py`. Browser integration can be reproduced with
`python tests/browser_id_screen.py` after installing Playwright, Chromium and
normal requirements. Set CHROMIUM_EXECUTABLE for a chosen binary and optionally
HN_SCREENSHOT_DIR for screenshot output. It uses local ports 8768/8769, the real
ASGI route and both real frontend scripts; only metadata HTTP is stubbed. It
checks default compatibility, submit/result separation, no automatic deeper
request, mobile width and JavaScript errors. No provider credential is required.

## Before release

This is a review implementation, not a merge or deployment. The provider
limiter/cache are single-process, not shared across workers or hosts. Add a
shared provider limit before multi-worker use. No fresh live metadata run,
absent-ID live control, OCR, author-year coverage, multi-worker traffic test,
production test or latency target is claimed. Document-wide extraction can miss
references, so the UI always warns that coverage may be incomplete. Full
bibliographic identity matching, DOI/PubMed and claim support are out of scope.

## Review evidence (2026-11-24)

- Repository test suite: 227 passed.
- Direct screening regressions: 39 passed.
- Both browser integration flows passed using stub metadata, not live arXiv.
- Desktop and mobile screenshots were inspected for both frontend variants.
