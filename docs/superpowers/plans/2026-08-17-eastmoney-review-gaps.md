# Eastmoney Guba Review Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` with the role split below. Steps use checkbox syntax for tracking.

**Goal:** Close the confirmed P1/P2 gaps in the merged Eastmoney Guba sentiment collector without changing shadow-mode Bili/Douyin scoring, coverage, alert state transitions, or the active frozen policy hash.

**Architecture:** Keep the Scrapy process as the public unauthenticated collection boundary. Add canonical evidence URLs and activity-aware bounded list pagination there; add a controlled representative-constituent provider and frozen provenance in the pipeline; resolve stale score display and promotion eligibility at the store/API orchestration boundary. Existing JSONL and SQLite contracts remain backward compatible.

**Tech Stack:** Python 3.12+, Scrapy 2.13, SQLite, FastAPI, React 19, TypeScript 6, pytest, Vitest.

## Global Constraints

- The primary `gpt-5.6-sol` agent writes all production code and design decisions.
- `gpt-5.6-terra` subagents write unit/regression tests, run regression reviews, and implement fixes for bugs confirmed by review.
- Every production behavior starts with a failing test that fails for the missing behavior, not a fixture or syntax error.
- Public unauthenticated Eastmoney access only: no login, CAPTCHA bypass, proxy rotation, cookies, or private endpoints.
- Preserve low-frequency quotas, bounded retries, circuit breaking, author hashing, text redaction, and the `guba.eastmoney.com` evidence URL allowlist.
- Shadow mode cannot change legacy Bili/Douyin scoring inputs, coverage qualification, alert state machine, or frozen policy hash.
- Promoted mode introduces the finance-community coverage gate only after 14 distinct quality-qualified shadow dates or an explicit audited manual override reason.
- Do not modify or restore uncommitted account-redaction changes in the parent workspace.

---

### Task 1: Canonical evidence URLs and activity-aware list pagination

**Files:**

- Modify: `apex/eastmoney_guba/parser.py`
- Modify: `apex/eastmoney_guba/spider.py`
- Modify: `apex/eastmoney_guba/pipeline.py`
- Modify: `apex/eastmoney_guba/exporter.py`
- Modify: `config.example.yaml`
- Test: `tests/test_eastmoney_guba.py`
- Test: `tests/test_eastmoney_pipeline.py`

**Interfaces:**

- `EastmoneyParser.parse_posts(...)` produces `published_at`, `last_activity_at`, and any public URL present in the source row.
- `build_frozen_manifest(...)` emits both first-page and `{page}` list templates plus the fixed window lower bound.
- `EastmoneySpider` counts only posts whose body timestamp is within the window, schedules comment requests for older posts whose last activity reaches the window, and schedules the next list page while the page can still contain in-window activity and the per-target post cap is not reached.
- `EastmoneyExporter.export(...)` requires a canonical `https://guba.eastmoney.com/news,...html` URL for both posts and comments.

- [x] Terra adds tests proving JSON posts and comments export real canonical public detail URLs.
- [x] Terra adds tests proving page 2 is scheduled when page 1 still contains activity in the 26-hour window and pagination stops at the lower bound or target cap.
- [x] Terra adds a test proving a pre-window post with an in-window top-level comment schedules comments but does not export/count the old post body.
- [x] Run the new tests and verify they fail for missing URLs/pagination/old-post scheduling.
- [x] Sol implements the minimal parser, manifest, spider, and exporter changes.
- [x] Run the focused tests and verify green without relaxing existing quota or allowlist assertions.

### Task 2: Default 20-trading-day representative constituent provider

**Files:**

- Create: `apex/eastmoney_guba/representatives.py`
- Modify: `apex/eastmoney_guba/models.py`
- Modify: `apex/eastmoney_guba/targets.py`
- Modify: `apex/eastmoney_guba/pipeline.py`
- Modify: `apex/sector_sentiment.py`
- Modify: `apex/eastmoney_guba/__init__.py`
- Test: `tests/test_eastmoney_pipeline.py`

**Interfaces:**

- `select_representative_constituents(taxonomy, trade_date, constituent_count=5, ...)` returns `(constituents_by_sector, provenance_by_sector)`.
- Each selected row contains `stock_code`, `turnover_20d`, `turnover_as_of`, and source identifiers.
- Each manifest contains `representative_constituents` entries with membership source, turnover source, requested count, selected count, 20-trading-day as-of, selected stocks, status, and an explicit shortfall reason when fewer than five are available.
- The default `run_configured` path calls this provider when no test/provider injection is supplied. Provider failure yields an explicit manifest shortfall and degraded Eastmoney status rather than silently collecting only sector forums.

- [x] Terra adds tests for deterministic top-five selection by summed turnover across exactly the latest 20 trading dates at or before `trade_date`.
- [x] Terra adds tests that the normal path invokes the controlled provider and freezes source/as-of/selected rows into the manifest.
- [x] Terra adds tests that provider failure or fewer than five valid stocks produces an explicit target shortfall and prevents a quality-qualified result.
- [x] Run the new tests and verify they fail for the current empty-config fallback.
- [x] Sol implements the controlled provider, manifest contract, and orchestration fallback.
- [x] Run focused tests and verify the existing injectable provider remains supported.

### Task 3: Stale score display and promotion eligibility

**Files:**

- Modify: `apex/eastmoney_guba/pipeline.py`
- Modify: `apex/sector_sentiment.py`
- Modify: `backend/routers/sector_sentiment.py`
- Modify: `frontend/src/api/sector-sentiment.ts`
- Modify: `frontend/src/routes/sector-sentiment/SectorSentimentPage.tsx`
- Modify: `config.example.yaml`
- Test: `tests/test_eastmoney_pipeline.py`
- Test: `tests/test_sector_sentiment_api.py`
- Test: `frontend/src/routes/sector-sentiment/__tests__/SectorSentimentPage.test.tsx`

**Interfaces:**

- `SentimentStore.eastmoney_shadow_progress()` remains the distinct attempt/qualified-date source of truth.
- Promotion is allowed when qualified dates are at least 14; otherwise configuration must carry a non-empty manual override reason that is copied to collection telemetry/audit.
- The store can resolve the latest successful Eastmoney score date at or before a failed attempt.
- Latest overview/detail responses expose the current failed collection audit while returning the previous successful score as stale display, including `stale: true` and `score_as_of`.
- If no stale score exists, a promoted collection failure reports degraded/insufficient data and cannot render as `normal` or otherwise imply low risk.

- [x] Terra adds tests for 13-day rejection, 14-day promotion, and an explicit reasoned override recorded in telemetry.
- [x] Terra adds API/store tests proving a failed current promoted run displays the previous successful score with current failure audit metadata.
- [x] Terra adds tests proving a promoted failure without fallback returns `insufficient_data`, not `normal`.
- [x] Terra adds a frontend test for stale score date/current failure copy.
- [x] Run the new tests and verify they fail for the current behavior.
- [x] Sol implements promotion decision, store lookup, API fallback, and frontend stale labeling.
- [x] Run focused backend/frontend tests and verify shadow hashes/scores/state tests remain unchanged.

### Task 4: Terra regression review and confirmed-bug fix wave

**Files:** Only files implicated by confirmed findings.

- [x] Terra runs the Eastmoney, sector sentiment, API, and frontend focused suites and reviews the complete diff against this plan.
- [x] Terra reports spec-compliance and code-quality verdicts with file/line evidence.
- [x] Terra implements tests plus fixes for every confirmed Critical/P1/P2 regression finding and reruns the covering suites.
- [x] Sol independently inspects the resulting diff and reruns the covering suites before accepting the fix wave.

### Task 5: Full verification

**Files:** No new behavior.

- [x] Run the complete backend pytest suite with a writable Numba cache.
- [x] Run the complete frontend Vitest suite.
- [x] Run the frontend TypeScript/Vite production build.
- [x] Run the Scrapy subprocess integration tests with loopback-only test hosts.
- [x] Verify the final diff does not contain credentials, salts, cookies, parent-workspace redaction edits, or non-allowlisted production URLs.
- [x] Record exact commands, pass/fail counts, and any environment-only limitations in the handoff.
