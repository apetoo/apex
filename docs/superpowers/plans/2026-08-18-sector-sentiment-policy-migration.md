# Sector Sentiment Policy Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit audited override that starts a new retrieval-policy cohort without deleting history or weakening the normal 60-day freeze.

**Architecture:** Extend `SentimentStore` with an append-only policy migration audit table and pass an optional override reason into policy reservation. The store remains the enforcement boundary: it validates chronology, same-day immutability, idempotency, and writes the migration plus reservation atomically before any collection side effects.

**Tech Stack:** Python, SQLite, pytest, YAML configuration.

**Spec:** `docs/superpowers/specs/2026-08-18-sector-sentiment-policy-migration-design.md`

## Global Constraints

- Preserve the default 60-distinct-trading-date freeze.
- Never delete or rewrite historical sentiment data.
- Never expose the free-form override reason through existing public API projections.
- Keep the override reason outside the retrieval policy hash.
- Tests and regression review are performed by a gpt-5.6-terra subtask.

---

### Task 1: Audited policy migration

**Files:**
- Modify: `apex/sector_sentiment.py`
- Test: `tests/test_sector_sentiment_hardening.py`

**Interfaces:**
- Consumes: `SentimentStore.reserve_policy_day(trade_date, config_hash)` and `run_configured(...)`.
- Produces: `SentimentStore.reserve_policy_day(trade_date, config_hash, override_reason="")` and an append-only `policy_migrations` SQLite table.

- [x] **Step 1: Write failing behavior tests**

Add tests that establish an initial incomplete cohort, then assert: no reason rejects a new hash; a non-empty reason accepts it on a later date; the prior rows remain unchanged; the audit row contains old/new hashes and cohorts; same-day conflict and historical override reject; retrying the same date and hash is idempotent.

- [x] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_sector_sentiment_hardening.py -k 'policy_override or policy_freeze'`

Expected: the new override tests fail because `reserve_policy_day` does not accept or persist an override.

- [x] **Step 3: Implement the minimal store migration**

Create `policy_migrations` during schema initialization. Extend cohort resolution/reservation so a non-empty override can create a new cohort only after the latest recorded date, and commit the migration audit and date reservation in one SQLite transaction. Preserve existing error behavior without an override.

- [x] **Step 4: Wire configuration at the orchestration boundary**

Read `sector_sentiment.policy_override_reason` in `run_configured` and pass it only to the pre-collection reservation. Do not add it to `_validate_retrieval_policy`'s frozen hash payload or API projections.

- [x] **Step 5: Verify GREEN and regressions**

Run the focused hardening tests, complete sector sentiment backend tests, Eastmoney pipeline/API regressions, and `git diff --check`.

- [x] **Step 6: Update operator documentation and commit**

Document the one-time migration configuration and removal-after-reservation behavior in the root usage guide, stage only scoped files, and commit with a policy-migration message.
