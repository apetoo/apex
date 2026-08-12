# Task 1 report: Eastmoney collector core

## Delivered

- Added isolated `apex.eastmoney_guba` package with target provider, parser, exporter,
  quota/result models, subprocess runner, and Scrapy spider process boundary.
- Public unauthenticated collection only: cookies are disabled and no login, proxy
  rotation, JavaScript, or CAPTCHA behavior exists.
- JSON parsing is preferred; fixed HTML fallbacks cover post rows and first-level
  comments. Access-control pages and contract drift are explicit exceptions.
- Exported JSONL uses the existing sector-sentiment ingest keys and salted author
  hashes; raw author identifiers are not emitted.
- Added `scrapy>=2.13,<2.14` to project dependencies and locked Scrapy 2.13.4.

## TDD evidence

### RED 1

Command: `pytest -q tests/test_eastmoney_guba.py`

Result: collection error, `ModuleNotFoundError: No module named 'apex.eastmoney_guba'`.
This was the expected failure because the collector package did not exist.

### GREEN 1

Command: `python -m pytest -q tests/test_eastmoney_guba.py`

Result: `8 passed`.

### RED 2

Command: `python -m pytest -q tests/test_eastmoney_guba.py`

Result after adding the comment-HTML fixture: one expected failure,
`SchemaChanged: comment HTML fallback schema not recognized`.

### GREEN 2

Command: `python -m pytest -q tests/test_eastmoney_guba.py`

Result: `9 passed`.

### Regression and runtime checks

- `python -m pytest -q tests/test_sector_sentiment.py tests/test_sector_sentiment_hardening.py`
  → `86 passed`.
- `uv run --frozen python -c "import scrapy; ..."`
  → `2.13.4 eastmoney_guba`.
- Full suite with a writable Numba cache: `379 passed, 19 failed`; all 19 failures
  are existing `tests/test_chan.py` failures because `czsc` is unavailable under the
  active Python 3.13 environment. Without a writable Numba cache, collection also
  hits the existing vectorbt/Numba cache-location error.

## Concerns / handoff

- Task 2 must supply frozen URL job manifests, window/cursor persistence, raw-response
  batch storage, and daily request limits to the runner. Task 1 intentionally does not
  invent or embed undocumented Eastmoney endpoint URLs.
- HTML selectors are contractual fixtures and should trigger `schema_changed`, not an
  empty-market interpretation, when Eastmoney changes markup.
- The target provider requires an explicit `eastmoney_forum_id`; it never guesses one.

## Review round 1 remediation

The initial spider only exposed a subprocess shell and was not acceptable as an
executable collector. The remediation wires the whole runtime path:

- A validated typed manifest supports `list`, `detail`, and `comments` jobs. List
  responses schedule detail and first-level comment requests, and every parsed record
  goes through `EastmoneyParser` and `EastmoneyExporter` into canonical JSONL.
- `QuotaBudget` now enforces total and per-target request limits; the spider also caps
  posts per target and comments per post. Denied scheduling sets `quota_exhausted`.
- 403, CAPTCHA markers, schema drift, and request failures are tracked separately.
  Reports are atomically replaced and never contain response bodies or author identity.
- Scrapy receives bounded retries, retry status codes, lower retry priority, timeout,
  randomized delay, AutoThrottle, and a manifest-scoped `JOBDIR`.
- Runner deletes stale reports before launch and fails closed on missing or malformed
  output.
- HTML nested replies are excluded using explicit parent-comment metadata.

### Review RED

Command: `.venv/bin/python -m pytest -q tests/test_eastmoney_guba.py`

Result: expected nested-comment and stale-report failures; process tests could not bind
localhost inside the filesystem sandbox. The process tests were then run with localhost
permission and continued to fail until the runtime wiring was implemented.

### Review GREEN

Command: `.venv/bin/python -m pytest -q tests/test_eastmoney_guba.py`

Result: `15 passed`, including successful list → detail/comments subprocess collection,
canonical JSONL, 403/CAPTCHA `blocked`, `schema_changed`, quota exhaustion, and stale
report fail-safe scenarios.

Command: `python -m pytest -q tests/test_sector_sentiment.py tests/test_sector_sentiment_hardening.py`

Result: `86 passed`. These existing tests use the project's dependency-complete system
environment; the new lock-derived minimal environment does not include the pre-existing
undeclared `httpx` dependency used by `apex.llm`, so combining those suites inside that
minimal environment produces dependency errors unrelated to this collector change.

## Review round 2 remediation

- Production manifests now accept HTTPS URLs only on the fixed
  `guba.eastmoney.com` / `gbapi.eastmoney.com` allowlist. Local HTTP fixtures require
  both `APEX_EASTMONEY_TESTING=1` and a loopback-only `test_hosts` declaration;
  `allowed_domains` is never derived from arbitrary manifest hosts.
- The custom retry middleware applies a non-blocking bounded exponential delay and
  counts retry requests against the same total/per-target quota.
- Comment pages expose typed `has_more`/`next_cursor` state, filter at the frozen time
  window, and stop scheduling at the configured maximum (hard-capped at 50).
- Duplicate typed URLs are premerged and carry every target/sector mapping into one
  canonical record. The configured per-domain concurrency now takes effect because the
  dead spider-level override was removed.
- Counts accept comma and Chinese ten-thousand forms while rejecting invalid/negative
  values. Budgets reject booleans, strings, fractional and negative values; zero remains
  an explicit valid stop budget. Recognized empty HTML containers are valid empty pages.
- Exporting is idempotent per batch/content/comment/sector set. A successfully completed
  `JOBDIR` is marked and cleared before an intentional rerun, while interrupted job state
  remains resumable. Reports remain atomic and stale-safe.

### Review round 2 RED/GREEN

Initial command: `.venv/bin/python -m pytest -q tests/test_eastmoney_guba.py`

RED: collection failed because `ExponentialRetryMiddleware` did not exist. Subsequent
focused RED exposed lost mappings after a manifest was validated twice and a completed
rerun reporting `empty_valid` instead of the stable batch result.

GREEN verification was split to stay below the desktop command's 30-second boundary:

- Non-network collector tests: `16 passed, 8 deselected`.
- Successful process/export and quota paths: `2 passed, 22 deselected`.
- Blocked/CAPTCHA/schema process paths: `3 passed, 21 deselected`.
- Bounded two-page/50-comment pagination: `1 passed`.
- Duplicate-URL multi-sector mapping: `1 passed`.
- Completed-batch rerun idempotency: `1 passed`.

## Review round 3 remediation

- A failed shared request now marks every merged target mapping failed, rather than
  attributing failure only to the first mapping.
- `QuotaBudget` itself rejects booleans, strings, fractional values, and negatives for
  both total and per-target limits; callers cannot bypass manifest validation.
- Comment pagination carries a set of already-seen comment identities. Overlapping pages
  neither duplicate JSONL nor consume the 50-comment cap twice.
- Merged mappings must share source type, stock code, and pool version, preventing the
  first mapping from silently classifying heterogeneous evidence.
- The real process test returns HTTP 500, verifies one bounded retry consumes the final
  request allowance, checks `quota_exhausted`, and confirms both merged targets fail.

### Review round 3 RED/GREEN

RED command: `.venv/bin/python -m pytest -q tests/test_eastmoney_guba.py -k 'quota_budget_strictly or mixed_mapping'`

Result: expected failures for string/fraction budgets and mixed classification.

GREEN commands/results:

- Strict quota and mapping validation: `5 passed, 25 deselected`.
- Shared 500/retry quota plus unique comment pagination process tests:
  `2 passed, 28 deselected`.
