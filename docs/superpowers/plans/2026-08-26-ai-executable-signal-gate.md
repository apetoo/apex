# AI Executable Signal Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate AI opinions from executable trades, honor conditional entries and strategy horizons, and produce frozen baseline/execution/challenger shadow metrics.

**Architecture:** Add a framework-independent decision module that validates AI proposals and returns a versioned trade decision. Pass screener lineage into analysis and persist it in journal. Extend the deterministic backtest with conditional entry simulation and a shadow comparison aggregator while preserving legacy APIs.

**Tech Stack:** Python 3, pandas, FastAPI/Pydantic, React/TypeScript, pytest, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-26-ai-executable-signal-gate-design.md`

## Global Constraints

- Work directly on `develop` as explicitly requested; do not create a worktree.
- Preserve append-only journal data and all existing compatibility fields.
- Write a failing regression test before every production behavior change.
- Do not add external data sources or a learned prediction model.
- Official win rate only includes filled and closed trades after all costs.

---

### Task 1: Versioned deterministic trade decision

**Files:**
- Create: `apex/trade_signal.py`
- Modify: `apex/schemas.py`, `config.example.yaml`
- Test: `tests/test_trade_signal.py`

**Interfaces:**
- Produces: `evaluate_trade_proposal(entry: dict, costs: dict | None = None) -> dict`
- Produces: `holding_period_for_setup(setup_tag: str | None) -> int | None`

- [ ] Write failing tests for strong verdict, watch verdict, missing fields, red flag, MA5 below, invalid plan, reward/risk below 1.3, and setup mappings.
- [ ] Run `.venv/bin/python -m pytest tests/test_trade_signal.py -q` and confirm failures are caused by the missing module.
- [ ] Implement the minimal pure decision module. Return `gate_version`, `proposed_action`, `action`, `eligible`, `reasons`, `holding_period_days`, `reward_risk_after_cost`, and normalized entry plan.
- [ ] Add `backtest.trade_signal_gate.mode` and immutable V1 defaults to the example config.
- [ ] Run the focused tests and commit `feat(analyze): 新增可执行信号质量门`.

### Task 2: Analysis contract and screener lineage

**Files:**
- Modify: `apex/analyze.py`, `apex/automation.py`, `backend/schemas/analyze.py`
- Test: `tests/test_analyze_evidence_contract.py`, `tests/test_automation.py`

**Interfaces:**
- Changes: `analyze.run(ts_code, save=True, on_progress=None, candidate_context=None)`
- Persists: `decision_schema_version`, `candidate_context`, `opinion_verdict`, `proposed_trade_action`, `trade_decision`

- [ ] Write failing tests proving AI tool schema requests `proposed_trade_action`, `entry_style`, and `valid_for_days`, and proving screener metadata reaches `analyze.run`.
- [ ] Run the focused tests and verify RED.
- [ ] Extend the tool schema and prompt to distinguish company outlook from immediate trade timing; abstain on critical gaps.
- [ ] Finalize the proposal through `evaluate_trade_proposal`, persist both proposal and authoritative decision, and retain `verdict`/`price_advice`.
- [ ] Pass each screener pick as `candidate_context`; mark direct calls as `manual` rather than losing provenance.
- [ ] Run focused analysis/automation tests and commit `feat(analyze): 贯通观点交易动作与粗筛来源`.

### Task 3: Conditional entry simulator and setup horizons

**Files:**
- Modify: `apex/backtest.py`
- Test: `tests/test_backtest_correctness.py`

**Interfaces:**
- Produces internal `_find_conditional_fill(entry, bars, as_of_date) -> dict`
- Extends status with `pending_entry` and `expired_unfilled`

- [ ] Write failing tests for open-in-band, pullback crossing, breakout crossing, gap-through rejection, three-day expiry, pending entry, limit-up inability, and T+1 exit after fill.
- [ ] Run focused tests and verify RED.
- [ ] Implement entry-window simulation before `_simulate_one` exit processing; count holding period from actual fill and use the setup mapping.
- [ ] Preserve legacy mode behavior when no versioned trade decision is requested.
- [ ] Run `tests/test_backtest_correctness.py` and commit `feat(backtest): 按建议区间与策略周期模拟成交`.

### Task 4: Frozen shadow comparison and metrics

**Files:**
- Modify: `apex/backtest.py`, `backend/routers/backtest.py`, `backend/schemas/backtest.py`
- Test: `tests/test_backtest_correctness.py`, `tests/test_screener_backtest.py`

**Interfaces:**
- Produces: `run_shadow(ts_code: str | None = None) -> dict`
- API: `GET /backtest/shadow`

- [ ] Write failing tests for baseline/execution/challenger denominators, gate coverage, fill coverage, completed-only win rate, average net return, profit factor, and split-first/split-last 30 metrics.
- [ ] Run focused tests and verify RED.
- [ ] Implement shared-row shadow aggregation without mutating journal; include analyzed, gate-passed, filled, completed, pending, expired and unfillable counts.
- [ ] Add API response mapping and retain existing signal/aggregate endpoints.
- [ ] Run focused backend tests and commit `feat(backtest): 增加新旧策略影子对照`.

### Task 5: Backtest UI comparison

**Files:**
- Modify: `frontend/src/api/backtest.ts`, `frontend/src/routes/backtest/BacktestPage.tsx`, `frontend/src/routes/backtest/stats.ts`
- Test: `frontend/src/routes/backtest/__tests__/BacktestStats.test.ts`

**Interfaces:**
- Adds `ShadowResult`, `ShadowArm`, and extended signal statuses to the API client.

- [ ] Write failing frontend tests for pending-entry/expired-unfilled exclusion and the three-arm comparison statistics.
- [ ] Run `cd frontend && npm test -- src/routes/backtest/__tests__/BacktestStats.test.ts` and verify RED.
- [ ] Add the shadow query and comparison card showing pass rate, fills, completed trades, win rate, average net return and profit factor.
- [ ] Display AI opinion, final trade action and rejection reasons without showing win/loss for non-final statuses.
- [ ] Run frontend tests and commit `feat(frontend): 展示可执行信号影子成绩`.

### Task 6: Verification and live-data audit

**Files:**
- Modify only if a regression test identifies a defect.

- [ ] Run `.venv/bin/python -m pytest tests/test_trade_signal.py tests/test_analyze_evidence_contract.py tests/test_backtest_correctness.py tests/test_screener_backtest.py -q`.
- [ ] Run `.venv/bin/python -m pytest -q` and document unrelated environmental or pre-existing failures without weakening assertions.
- [ ] Run `cd frontend && npm test`, `cd frontend && npm run lint`, and `npm run build`.
- [ ] Recompute current journal in shadow mode and verify legacy results remain available while legacy records are not misclassified as V1 trades.
- [ ] Review `git diff`, confirm no local journal/config/token files are staged, and commit any final test-only correction with a conventional Chinese commit message.
