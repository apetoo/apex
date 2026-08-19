# Intelligent Evidence Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a LangGraph-orchestrated, quality-gated stock research loop that can either produce a reviewed conclusion or abstain safely.

**Architecture:** Pure Python evidence-quality and budget modules enforce domain-independent invariants. A LangGraph `StateGraph` routes existing model/tool calls through safety scan, gap assessment, draft, independent review, one optional rework, and finalization. Existing business guards and persistence remain authoritative, with the legacy loop available as a temporary fallback.

**Tech Stack:** Python 3.12, LangGraph 1.x, OpenAI-compatible DeepSeek client, pytest, React/TypeScript frontend.

**Spec:** `docs/superpowers/specs/2026-08-19-intelligent-evidence-research-design.md`

## Global Constraints

- Preserve existing tool implementations and trading-domain guards.
- No new data provider.
- Default budget: 300 seconds, 12 external calls, three research rounds, two no-progress rounds, one review rework.
- Do not expose or persist hidden chain-of-thought.
- Existing journal records remain readable.

---

### Task 1: Search quality and evidence records

**Files:** create `apex/evidence.py`; modify `apex/data.py`, `apex/schemas.py`; test `tests/test_evidence_quality.py`.

- [ ] Add failing fixtures for entity mismatch, URL-derived tiers, category mismatch, future dates, and duplicate reposts.
- [ ] Implement normalized search results and structured `EvidenceItem` creation.
- [ ] Make `web_search` include name plus both code forms and return only accepted results with quality telemetry.
- [ ] Run the focused tests and commit the independently working search-quality slice.

### Task 2: Evidence controller and abstention persistence

**Files:** create `apex/evidence_control.py`; modify `apex/journal.py`, `apex/schemas.py`; test `tests/test_evidence_control.py`, `tests/test_journal_loaders.py`.

- [ ] Add failing tests for budgets, no-progress convergence, critical unknowns, source sufficiency, and abstention isolation.
- [ ] Implement controller state transitions and finalization gates.
- [ ] Add completed/insufficient result schemas and legacy defaults.
- [ ] Run focused tests and commit the controller slice.

### Task 3: LangGraph orchestration

**Files:** create `apex/analysis_graph.py`; modify `apex/analyze.py`, `pyproject.toml`, `config.example.yaml`; test `tests/test_analysis_graph.py` and existing position-action tests.

- [ ] Add failing graph-routing tests for clean completion, tool research, pass, rework, abstain, and trim/exit blocking.
- [ ] Add LangGraph dependency and implement a custom `StateGraph`; do not use a prebuilt ReAct agent.
- [ ] Adapt existing OpenAI-compatible messages/tools, progress events, final business guards, and trace output.
- [ ] Keep the legacy loop behind `analysis.orchestrator=legacy`; default to `langgraph`.
- [ ] Run focused analysis and position-action tests and commit the orchestration slice.

### Task 4: CLI and frontend output

**Files:** modify `main.py`, the analysis API response types, and analysis result UI; add Python and frontend tests at the existing seams.

- [ ] Add failing tests for the insufficient-evidence CLI/API/UI state.
- [ ] Render status, unknowns, and research summary while hiding direction, prices, and actions.
- [ ] Verify completed and legacy records retain their current presentation.
- [ ] Run focused backend and frontend tests and commit the presentation slice.

### Task 5: Verification and review

- [ ] Replay the fixed bad-search fixture and record quality telemetry.
- [ ] Run all focused Python and frontend tests.
- [ ] Run the full suite, separately reporting documented baseline failures caused by sandbox sockets, missing local config, and unrelated sector-sentiment tests.
- [ ] Review the diff for spec compliance and structural risks, fix findings test-first, and commit the final result.
