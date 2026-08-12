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
