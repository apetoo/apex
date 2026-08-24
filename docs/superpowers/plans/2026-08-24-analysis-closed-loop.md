# AI Analysis Closed Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LangGraph deterministically finalize evidence-ready stock analyses and return actionable structured abstentions for genuine failures.

**Architecture:** `EvidenceController` remains the safety authority. The graph routes evidence-ready state into a dedicated forced-draft node, validates the candidate with existing trading guards, and classifies any terminal failure separately from business evidence gaps. Existing SSE, journal, CLI, and React components consume additive fields.

**Tech Stack:** Python 3.12, LangGraph, Click, FastAPI SSE, React, TypeScript, Vitest

**Spec:** `docs/superpowers/specs/2026-08-24-analysis-closed-loop-design.md`

## Global Constraints

- Preserve `analysis_status=completed|insufficient_evidence`.
- Do not weaken evidence, price, valuation, position, review, or trading-isolation guards.
- Preserve existing uncommitted workspace changes and avoid unrelated refactors.
- Add no model, data-source, or LangGraph persistence dependency.

---

### Task 1: Deterministic graph finalization and outcome contract

**Files:** `apex/analysis_graph.py`, `apex/analyze.py`, `apex/evidence_control.py`, `apex/schemas.py`, backend tests.

- [ ] Add failing tests for evidence-ready forced draft, concrete evidence-gap abstention, provider failure, draft retry, review failure, and held/unheld tool selection.
- [ ] Extend graph state and routing so evidence readiness, not model tool choice, enters draft.
- [ ] Implement restricted draft generation and reuse existing candidate validation.
- [ ] Add outcome reason, next actions, research metrics, and dynamic summary to abstention results.
- [ ] Run focused backend tests and refactor only after green.

### Task 2: CLI and SSE business progress

**Files:** `main.py`, analysis event emitters, CLI/trace tests.

- [ ] Add failing tests for structured status events and localized CLI failures.
- [ ] Emit preparing, safety_scan, researching, assessing, drafting, reviewing, completed, and abstained stages with round progress.
- [ ] Consume progress in CLI and render actionable failure details without internal enums.
- [ ] Run focused backend tests.

### Task 3: Frontend actionable abstention experience

**Files:** analyze API types, `AnalyzeTraceStream`, `VerdictDetailCard`, `LatestAnalysisBadge`, component tests.

- [ ] Add failing tests for stage progress, evidence-gap/provider-failure copy, evidence/actions/metrics rendering, and hidden trading controls.
- [ ] Extend additive TypeScript types and render current business stage while trace stays collapsed.
- [ ] Expand abstention cards and historical badges with localized outcome labels.
- [ ] Run frontend tests and production build.

### Task 4: Regression and live acceptance

- [ ] Run the full affected Python and frontend suites.
- [ ] Run production frontend build.
- [ ] Execute `603893.SH` with live progress and no-save; confirm no internal stop code appears in user unknowns.
- [ ] Review the final diff against the spec and document any live external-service limitation.
