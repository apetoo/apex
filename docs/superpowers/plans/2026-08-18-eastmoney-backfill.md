# Eastmoney Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a narrow, auditable CLI for Eastmoney-only historical collection with an explicit opt-in to count a qualified backfill as a shadow day.

**Architecture:** A new `apex.eastmoney_guba.backfill` module owns CLI validation and invokes a small Eastmoney-only service seam rather than `run_configured`, so short-video jobs and their budgets are never created. The pipeline receives an explicit collection timestamp for the frozen historical window; a countable backfill writes a separately audited collection run only after existing Eastmoney quality gates pass.

**Tech Stack:** Python 3.12, argparse, existing Eastmoney pipeline/Scrapy runner, SQLite `SentimentStore`, pytest.

## Global Constraints

- Require public, unauthenticated Eastmoney access; preserve URL allowlist, quotas and author redaction.
- `--trade-date` and timezone-aware `--as-of` are mandatory.
- Default backfill does not alter Bili/Douyin data, score state, normal budgets, or shadow progress.
- `--count-shadow-day` requires a non-empty `--reason` and is forbidden when Eastmoney is promoted.
- Tests are written and observed failing before each production change.

---

### Task 1: Validate the backfill CLI contract

**Files:**
- Create: `apex/eastmoney_guba/backfill.py`
- Test: `tests/test_eastmoney_backfill.py`

**Interfaces:**
- Produces: `parse_backfill_args(argv: list[str]) -> argparse.Namespace`
- Produces: `validate_backfill_request(trade_date: str, as_of: str, count_shadow_day: bool, reason: str, phase: str) -> None`

- [ ] **Step 1: Write the failing tests**

```python
def test_backfill_requires_timezone_aware_window_and_reason_for_shadow_count():
    with pytest.raises(ValueError, match="timezone"):
        validate_backfill_request("2026-08-17", "2026-08-17T20:00:00", False, "", "shadow")
    with pytest.raises(ValueError, match="reason"):
        validate_backfill_request("2026-08-17", "2026-08-17T20:00:00+08:00", True, "", "shadow")
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest -q tests/test_eastmoney_backfill.py`

- [ ] **Step 3: Implement validation and argparse**

```python
parser.add_argument("--trade-date", required=True)
parser.add_argument("--as-of", required=True)
parser.add_argument("--count-shadow-day", action="store_true")
parser.add_argument("--reason", default="")
parser.add_argument("--offline-replay", action="store_true")
```

- [ ] **Step 4: Run GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_eastmoney_backfill.py`

### Task 2: Run Eastmoney-only backfill and persist audit outcome

**Files:**
- Modify: `apex/sector_sentiment.py`
- Modify: `apex/eastmoney_guba/pipeline.py`
- Modify: `apex/eastmoney_guba/backfill.py`
- Test: `tests/test_eastmoney_backfill.py`

**Interfaces:**
- Produces: `run_eastmoney_backfill(cfg: dict, *, trade_date: str, collected_at: str, count_shadow_day: bool, reason: str, offline_replay: bool) -> dict`

- [ ] **Step 1: Write the failing tests**

```python
def test_default_backfill_runs_only_eastmoney_and_does_not_add_shadow_progress(tmp_path):
    result = run_eastmoney_backfill(config(tmp_path), trade_date="2026-08-17", collected_at="2026-08-17T20:00:00+08:00", count_shadow_day=False, reason="", offline_replay=False)
    assert result["backfill"] is True
    assert result["counted_shadow_day"] is False
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest -q tests/test_eastmoney_backfill.py::test_default_backfill_runs_only_eastmoney_and_does_not_add_shadow_progress`

- [ ] **Step 3: Implement the narrow service seam**

```python
def run_eastmoney_backfill(...):
    # select representatives, call collect_eastmoney with collected_at,
    # then persist only an explicitly qualified counted backfill.
```

- [ ] **Step 4: Run GREEN and existing pipeline tests**

Run: `.venv/bin/python -m pytest -q tests/test_eastmoney_backfill.py tests/test_eastmoney_pipeline.py`

### Task 3: Offline replay and operational documentation

**Files:**
- Modify: `apex/eastmoney_guba/backfill.py`
- Modify: `EASTMONEY_GUBA_USAGE.md`
- Test: `tests/test_eastmoney_backfill.py`

**Interfaces:**
- Consumes: frozen manifest and saved `responses/*.body` from a prior batch.

- [ ] **Step 1: Write the failing offline-replay test**

```python
def test_offline_replay_rejects_missing_saved_batch(tmp_path):
    with pytest.raises(FileNotFoundError, match="saved Eastmoney batch"):
        run_eastmoney_backfill(config(tmp_path), trade_date="2026-08-17", collected_at="2026-08-17T20:00:00+08:00", count_shadow_day=False, reason="", offline_replay=True)
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest -q tests/test_eastmoney_backfill.py::test_offline_replay_rejects_missing_saved_batch`

- [ ] **Step 3: Implement replay and document exact commands**

```python
# Replay must use the stored manifest's allowlisted targets and write a new auditable batch.
```

- [ ] **Step 4: Run final verification**

Run: `.venv/bin/python -m pytest -q tests/test_eastmoney_backfill.py tests/test_eastmoney_pipeline.py tests/test_eastmoney_guba.py`
