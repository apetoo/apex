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
