# Backtest Signal Deduplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the public per-signal backtest use the same same-stock/same-day deduplication policy as sweep, aggregate, calibration inputs, and portfolio backtests.

**Architecture:** Keep Journal append-only and reuse `apex.backtest._dedupe_signals()` at the `run()` public boundary. The internal simulator `_run_entries()` remains a deterministic executor over exactly the entries supplied by its caller.

**Tech Stack:** Python 3.12, pandas, pytest, pytest monkeypatch.

**Spec:** `docs/superpowers/specs/2026-08-26-backtest-signal-deduplication-design.md`

## Global Constraints

- Do not delete or rewrite Journal records.
- Use `(ts_code, date)` as the simulated-signal identity.
- Keep the record with the greatest non-empty `analyzed_at` value.
- Preserve records without `date` because they cannot be deduplicated reliably.
- Do not change API fields, AI prompts, selection rules, exits, or holding periods.

---

### Task 1: Apply Existing Deduplication to Per-Signal Backtest

**Files:**
- Modify: `apex/backtest.py:666-687`
- Test: `tests/test_backtest_correctness.py`

**Interfaces:**
- Consumes: `_dedupe_signals(entries: list) -> list`, which already implements the approved latest-analysis-wins policy.
- Produces: `run(ts_code: Optional[str] = None, lookforward_days: Optional[int] = None, include_benchmark: bool = True) -> pd.DataFrame` with at most one dated row per `(ts_code, date)`.

- [x] **Step 1: Write the failing regression test**

Add this test to `tests/test_backtest_correctness.py`:

```python
def test_run_dedupes_same_stock_same_day_before_simulation(monkeypatch):
    older = {
        **_entry(),
        "analyzed_at": "2026-08-01T10:00:00+08:00",
        "confidence": 2,
    }
    newer = {
        **_entry(),
        "analyzed_at": "2026-08-01T14:00:00+08:00",
        "confidence": 7,
    }
    simulated = []

    monkeypatch.setattr(bt.journal, "load_verdicts", lambda **kwargs: [older, newer])
    monkeypatch.setattr(bt, "_prefetch_signals", lambda *args, **kwargs: None)

    def capture(entries, holding_period, include_benchmark):
        simulated.extend(entries)
        return entries, {
            "completed": 0,
            "pending": 0,
            "data_truncated": 0,
            "unfillable": 0,
            "no_fill_data": 0,
        }

    monkeypatch.setattr(bt, "_run_entries", capture)

    result = bt.run(lookforward_days=10, include_benchmark=False)

    assert len(result) == 1
    assert len(simulated) == 1
    assert simulated[0]["analyzed_at"] == newer["analyzed_at"]
    assert simulated[0]["confidence"] == 7
```

- [x] **Step 2: Run the regression test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_backtest_correctness.py::test_run_dedupes_same_stock_same_day_before_simulation -q
```

Expected: FAIL because `run()` passes both records to `_run_entries()`, so `len(result)` and `len(simulated)` are 2.

- [x] **Step 3: Implement the minimal production change**

In `apex/backtest.py`, update `run()` immediately after bullish filtering:

```python
entries = journal.load_verdicts(ts_code=ts_code)
long_entries = [e for e in entries if _is_bullish(e.get("verdict", ""))]
long_entries = _dedupe_signals(long_entries)
```

Do not move deduplication into `_run_entries()` and do not alter `_dedupe_signals()`.

- [x] **Step 4: Verify GREEN and regression coverage**

Run:

```bash
.venv/bin/python -m pytest tests/test_backtest_correctness.py::test_run_dedupes_same_stock_same_day_before_simulation -q
.venv/bin/python -m pytest tests/test_backtest_correctness.py tests/test_screener_backtest.py -q
```

Expected: the focused test passes, then all listed backtest tests pass.

- [x] **Step 5: Verify current Journal statistics use one denominator**

Run a read-only Python check that computes `backtest.run(lookforward_days=10)` and `backtest.aggregate(lookforward_days=10)`. Assert that the number of `completed` plus `data_truncated` rows from `run()` equals `aggregate()["fillable_count"]`; for the current Journal both are expected to be 105.

- [ ] **Step 6: Commit the implementation**

```bash
git add apex/backtest.py tests/test_backtest_correctness.py docs/superpowers/plans/2026-08-26-backtest-signal-deduplication.md
git commit -m "fix(backtest): 统一逐笔信号去重口径"
```
