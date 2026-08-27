# Position Action Effective State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ambiguous position-action patch/snapshot hybrid with a deterministic proposal-to-effective-state pipeline shared by review, report validation, and persistence.

**Architecture:** Add a small framework-independent domain module that adapts legacy proposals and materializes a complete effective position state from a frozen baseline. Keep `draft_proposal` and `draft_data` separate in LangGraph: the former records requested changes, while the latter is the sole effective-state source consumed by review, reporting, validation, and final persistence.

**Tech Stack:** Python 3.12, `TypedDict`, FastAPI service-layer conventions, LangGraph, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-position-action-effective-state-design.md`

## Global Constraints

- Normalize stock codes only at existing system boundaries; this change does not introduce a new stock-code boundary.
- `new_stop` and `new_target` missing from a proposal mean “preserve the baseline value”.
- `ladder_intent` is exactly one of `preserve`, `replace`, or `clear`.
- `preserve` and `clear` reject submitted ladder levels; `replace` requires a non-empty complete ladder.
- A legacy missing or empty `scale_plan` adapts to `preserve`; a legacy non-empty `scale_plan` adapts to `replace`.
- Only the materialized effective state may feed independent review, formal reporting, deterministic report validation, and watchlist persistence.
- A real position closed during analysis still blocks persistence; this plan does not weaken the existing race guard.
- Old journal and watchlist records remain readable; no migration or historical rewrite is added.
- Do not modify frontend behavior or unrelated evidence/research logic.

---

## File Map

- Create `apex/position_action_state.py`: domain types, legacy adapter, deterministic materializer, and intent-shape validation.
- Create `tests/test_position_action_state.py`: focused unit coverage for all proposal and ladder-intent semantics.
- Modify `apex/analysis_graph.py`: register `position_baseline` and `draft_proposal` in graph state.
- Modify `apex/analyze.py`: freeze the baseline, prepare proposals, validate effective state, pass the effective state through review/report, and finalize proposal plus effective state.
- Modify `tests/test_analysis_graph.py`: graph-level `revise → pass → report` and authoritative-context regressions.
- Modify `tests/test_position_action_dispatch.py`: proposal validation and compatibility-adapter coverage at the real business guard.
- Modify `tests/test_position_action_finalize.py`: persistence uses effective values without treating preserved values as new advice.

---

### Task 1: Position-action domain materializer

**Files:**
- Create: `apex/position_action_state.py`
- Create: `tests/test_position_action_state.py`

**Interfaces:**
- Consumes: baseline dictionaries shaped like active watchlist positions and raw `record_position_action` argument dictionaries.
- Produces: `adapt_legacy_proposal(raw: dict) -> dict`, `validate_proposal_shape(proposal: dict) -> list[str]`, and `materialize_effective_position_plan(baseline: dict, proposal: dict) -> dict`.

- [ ] **Step 1: Write failing adapter and materialization tests**

Create tests with explicit expected dictionaries:

```python
from apex.position_action_state import (
    adapt_legacy_proposal,
    materialize_effective_position_plan,
    validate_proposal_shape,
)


BASELINE = {
    "stop_loss": 71.5,
    "target": 90.0,
    "plan": {"scale_plan": [
        {"level": 1, "action": "trim", "trigger_price": 71.5, "pct": 1.0},
        {"level": 2, "action": "add", "trigger_price": 79.0, "shares": 100,
         "new_stop": 73.0},
    ]},
}


def test_preserve_materializes_complete_effective_state():
    proposal = {"action": "hold", "ladder_intent": "preserve", "rationale": "维持"}
    result = materialize_effective_position_plan(BASELINE, proposal)
    assert result["effective_stop"] == 71.5
    assert result["effective_target"] == 90.0
    assert result["effective_scale_plan"] == BASELINE["plan"]["scale_plan"]
    assert result["ladder_intent"] == "preserve"


def test_replace_and_clear_are_explicit():
    replacement = [{"action": "trim", "trigger_price": 75.0, "pct": 0.5}]
    replaced = materialize_effective_position_plan(BASELINE, {
        "action": "hold", "ladder_intent": "replace", "scale_plan": replacement,
    })
    cleared = materialize_effective_position_plan(BASELINE, {
        "action": "hold", "ladder_intent": "clear",
    })
    assert replaced["effective_scale_plan"] == replacement
    assert cleared["effective_scale_plan"] == []


def test_legacy_empty_plan_is_safe_preserve_and_nonempty_is_replace():
    assert adapt_legacy_proposal({"action": "hold", "scale_plan": []})["ladder_intent"] == "preserve"
    assert adapt_legacy_proposal({
        "action": "hold", "scale_plan": [{"action": "add", "trigger_price": 79, "shares": 100}],
    })["ladder_intent"] == "replace"


def test_intent_shape_rejects_ambiguous_combinations():
    assert validate_proposal_shape({
        "action": "hold", "ladder_intent": "preserve", "scale_plan": [{"level": 1}],
    }) == ["ladder_intent=preserve 时不得提交 scale_plan 档位。"]
    assert validate_proposal_shape({
        "action": "hold", "ladder_intent": "replace", "scale_plan": [],
    }) == ["ladder_intent=replace 时 scale_plan 必须是非空完整计划；清空请使用 clear。"]
```

Also cover `new_stop/new_target` replacement, deep-copy isolation, `exit` clearing the effective ladder, and invalid intent values.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_position_action_state.py`

Expected: collection fails because `apex.position_action_state` does not exist.

- [ ] **Step 3: Implement the focused domain module**

Use `copy.deepcopy` so neither baseline nor proposal is mutated. Define narrow types and implement the three public functions:

```python
from copy import deepcopy
from typing import Literal, TypedDict

LadderIntent = Literal["preserve", "replace", "clear"]


class EffectivePositionPlan(TypedDict, total=False):
    action: str
    add_shares: int | None
    trim_shares: int | None
    trim_pct: float | None
    effective_stop: float | None
    effective_target: float | None
    effective_scale_plan: list[dict]
    ladder_intent: LadderIntent
    rationale: str
    change_summary: dict[str, str]


def adapt_legacy_proposal(raw: dict) -> dict:
    proposal = deepcopy(raw or {})
    if "ladder_intent" not in proposal:
        proposal["ladder_intent"] = "replace" if proposal.get("scale_plan") else "preserve"
        if proposal["ladder_intent"] == "preserve":
            proposal.pop("scale_plan", None)
    return proposal
```

`materialize_effective_position_plan` must call `validate_proposal_shape` and raise `ValueError("；".join(issues))` when invalid. For `preserve`, copy `baseline["plan"]["scale_plan"]`; for `replace`, copy proposal levels; for `clear` or `action=exit`, use `[]`. Preserve baseline stop/target when proposal fields are absent. Record `change_summary` values as `preserved`, `replaced`, or `cleared`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_position_action_state.py`

Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add apex/position_action_state.py tests/test_position_action_state.py
git commit -m "feat(analyze): 建立持仓动作有效状态模型"
```

---

### Task 2: Proposal preparation and business validation

**Files:**
- Modify: `apex/analysis_graph.py:11-44`
- Modify: `apex/analyze.py:426-480, 1087-1115, 1987-2251, 3137-3245`
- Modify: `tests/test_position_action_dispatch.py`
- Modify: `tests/test_analysis_graph.py`

**Interfaces:**
- Consumes: Task 1 functions and a frozen active-position baseline.
- Produces: graph state keys `position_baseline: dict` and `draft_proposal: dict`; helper `_prepare_position_action_candidate(tool_input: dict, baseline: dict, ts_code: str, current_price) -> tuple[dict, dict, list[str]]`, returning `(proposal, effective, blockers)`.

- [ ] **Step 1: Write failing proposal-preparation tests**

Add dispatch tests showing the real guard receives an effective plan:

```python
def _held_position_with_plan():
    return {
        "ts_code": "000977.SZ", "entry_price": 77.095,
        "position_size_shares": 200, "stop_loss": 71.5, "target": 90.0,
        "plan": {"scale_plan": [
            {"level": 1, "action": "trim", "trigger_price": 71.5,
             "pct": 1.0, "reason": "防守退出"},
            {"level": 2, "action": "add", "trigger_price": 79.0,
             "shares": 100, "new_stop": 73.0, "reason": "突破确认"},
        ]},
    }


def test_legacy_empty_ladder_preserves_baseline_in_effective_candidate(isolated_paths):
    baseline = _held_position_with_plan()
    proposal, effective, blockers = analyze._prepare_position_action_candidate(
        {"action": "hold", "scale_plan": [], "rationale": "维持原计划"},
        baseline, "000977.SZ", 78.27,
    )
    assert blockers == []
    assert proposal["ladder_intent"] == "preserve"
    assert effective["effective_stop"] == 71.5
    assert effective["effective_target"] == 90.0
    assert len(effective["effective_scale_plan"]) == 2


def test_explicit_clear_and_replace_are_validated_before_review(isolated_paths):
    _, _, clear_blockers = analyze._prepare_position_action_candidate(
        {"action": "hold", "ladder_intent": "clear", "rationale": "取消条件单"},
        _held_position_with_plan(), "000977.SZ", 78.27,
    )
    _, _, invalid_blockers = analyze._prepare_position_action_candidate(
        {"action": "hold", "ladder_intent": "replace", "scale_plan": []},
        _held_position_with_plan(), "000977.SZ", 78.27,
    )
    assert clear_blockers == []
    assert any("清空请使用 clear" in item for item in invalid_blockers)
```

Add a graph test asserting an accepted `record_position_action` stores the adapted proposal in `draft_proposal` and the complete effective result in `draft_data`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_position_action_dispatch.py tests/test_analysis_graph.py -k 'effective_candidate or explicit_clear or adapted_proposal'`

Expected: failures because `_prepare_position_action_candidate` and the new state fields do not exist.

- [ ] **Step 3: Register state and freeze the baseline**

Add these fields to `AnalysisGraphState`:

```python
position_baseline: dict[str, Any]
draft_proposal: dict[str, Any]
```

In `run()`, load the matching active position once, pass it into `_format_portfolio_context` and `_run_langgraph_loop`, and keep the existing live reload inside the final race guard. Extend `_run_langgraph_loop` with:

```python
position_baseline: dict | None = None,
```

Initialize it in `prepare` as a deep copy. Do not derive it again after reviewer revision.

- [ ] **Step 4: Implement common candidate preparation**

Implement `_prepare_position_action_candidate` as the only position-action conversion boundary:

```python
proposal = adapt_legacy_proposal(tool_input)
shape_issues = validate_proposal_shape(proposal)
if shape_issues:
    return proposal, {}, shape_issues
effective = materialize_effective_position_plan(baseline, proposal)
validation_input = {
    **proposal,
    "new_stop": effective["effective_stop"],
    "new_target": effective["effective_target"],
    "scale_plan": effective["effective_scale_plan"],
}
reasons, _ = _validate_position_action(
    validation_input, ts_code, [], current_price=current_price,
)
return proposal, effective, reasons
```

Use this helper in both `execute_tools` and forced `draft`. Store proposal and effective state separately. Update the tool JSON schema so `ladder_intent` is required for new model calls and documents all three exact values; keep the adapter for old tests and external callers.

- [ ] **Step 5: Run focused and existing dispatch tests**

Run: `.venv/bin/python -m pytest -q tests/test_position_action_state.py tests/test_position_action_dispatch.py tests/test_analysis_graph.py -k 'position or ladder or proposal or candidate'`

Expected: all selected tests pass; existing ladder path rejection behavior remains unchanged.

- [ ] **Step 6: Commit Task 2**

```bash
git add apex/analysis_graph.py apex/analyze.py tests/test_position_action_dispatch.py tests/test_analysis_graph.py
git commit -m "refactor(analyze): 物化持仓动作候选状态"
```

---

### Task 3: Single authoritative state across review and reporting

**Files:**
- Modify: `apex/analyze.py:2348-2584`
- Modify: `tests/test_analysis_graph.py`

**Interfaces:**
- Consumes: `state["position_baseline"]`, `state["draft_proposal"]`, and Task 2 `state["draft_data"]` effective result.
- Produces: reviewer context `{baseline, proposal, effective}` and formal report/validator inputs sourced exclusively from the effective result.

- [ ] **Step 1: Write the exact 000977 failing graph regression**

Model the captured trace: first proposal changes stop/target/ladder and receives `revise`; second legacy proposal says hold with an empty plan and receives `pass`; the formal report repeats baseline values and ladder.

```python
def _held_000977_with_two_level_plan():
    return {
        "ts_code": "000977.SZ", "entry_price": 77.095,
        "position_size_shares": 200, "stop_loss": 71.5, "target": 90.0,
        "plan": {"scale_plan": [
            {"level": 1, "action": "trim", "trigger_price": 71.5,
             "pct": 1.0, "reason": "防守退出"},
            {"level": 2, "action": "add", "trigger_price": 79.0,
             "shares": 100, "new_stop": 73.0, "reason": "突破确认"},
        ]},
    }


def _effective_position_report():
    return _complete_position_report().replace(
        "**当前动作：hold**，保持现有仓位并执行既定风险计划。",
        "**当前动作：hold**\n**当前有效止损：71.5**\n**当前有效目标：90.0**",
    ).replace(
        "价格满足计划条件后才执行未来动作，当前不提前交易。",
        "- trim @ 71.5，比例 1.0\n- add @ 79.0，100 股，新止损 73.0",
    )


def test_000977_revision_preserves_effective_plan_for_report(monkeypatch):
    baseline = _held_000977_with_two_level_plan()
    first = {
        "action": "hold", "new_stop": 73.0, "new_target": 90.0,
        "ladder_intent": "replace", "scale_plan": [
            {"level": 1, "action": "trim", "trigger_price": 73.0, "pct": 1.0},
            {"level": 2, "action": "add", "trigger_price": 79.0,
             "shares": 100, "new_stop": 74.0},
        ],
        "rationale": "上移止损",
    }
    second_legacy = {"action": "hold", "scale_plan": [], "rationale": "维持原计划"}
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "维持持仓", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_position_action", arguments=first),
        _response(content=json.dumps({
            "outcome": "pass", "issues": [
                {"message": "技术描述需修订", "severity": "minor", "blocking": False},
            ],
        }, ensure_ascii=False)),
        _response(tool_name="record_position_action", arguments=second_legacy),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=_effective_position_report()),
    ])
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": [baseline]})
    monkeypatch.setattr(analyze.data, "get_realtime_price", lambda _codes: {"000977.SZ": 78.27})
    monkeypatch.setattr(analyze.journal, "load_position_actions", lambda _code: [])
    result = analyze._run_langgraph_loop(
        ts_code="000977.SZ", client=client, model="fake", messages=[],
        max_iter=12, emit=lambda _event: None, position_baseline=baseline,
    )
    assert result["analysis_status"] == "completed"
    assert result["draft_data"]["effective_stop"] == 71.5
    assert result["draft_data"]["effective_target"] == 90.0
    assert len(result["draft_data"]["effective_scale_plan"]) == 2
```

Assert the reviewer prompt includes separate `baseline`, `proposal`, and `effective`, and the report prompt labels only `effective` as the final machine result.

- [ ] **Step 2: Run the regression and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_analysis_graph.py::test_000977_revision_preserves_effective_plan_for_report`

Expected: FAIL with the captured report mismatch or missing effective-state fields.

- [ ] **Step 3: Update reviewer state transitions**

Build reviewer context as:

```python
compact = {
    "candidate_kind": state.get("draft_kind"),
    "baseline": state.get("position_baseline") or {},
    "proposal": state.get("draft_proposal") or {},
    "effective": state.get("draft_data") or {},
    # existing evidence, gaps, safety status, and system context follow
}
```

On `revise` or `rework`, clear both `draft_proposal` and `draft_data`. Preserve `position_baseline`. Rewrite reviewer instructions so it evaluates the effective result and uses proposal only to explain changes.

- [ ] **Step 4: Update formal report and deterministic validator inputs**

For position actions, stop reading `new_stop`, `new_target`, and `scale_plan` as final values. Build a report candidate with:

```python
report_candidate = {
    **effective,
    "current_stop": effective.get("effective_stop"),
    "current_target": effective.get("effective_target"),
    "scale_plan": effective.get("effective_scale_plan") or [],
}
```

Require report labels `当前有效止损` and `当前有效目标` when their values are not `None`. Validate the complete effective ladder. For `ladder_intent=clear`, require a clear-plan statement and zero parsed levels. The historical baseline may be included only under a non-authoritative `baseline_for_change_explanation` key.

- [ ] **Step 5: Run graph regressions and verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_analysis_graph.py`

Expected: all graph tests pass, including the exact 000977 revision path and existing verdict paths.

- [ ] **Step 6: Commit Task 3**

```bash
git add apex/analyze.py tests/test_analysis_graph.py
git commit -m "fix(analyze): 统一持仓报告权威状态"
```

---

### Task 4: Persistence compatibility and end-to-end verification

**Files:**
- Modify: `apex/analyze.py:2876-2889, 3250-3365`
- Modify: `tests/test_position_action_finalize.py`
- Modify: `tests/test_analysis_graph.py`

**Interfaces:**
- Consumes: final `draft_proposal` and `draft_data` effective state.
- Produces: journal entries retaining legacy change fields plus explicit effective fields; watchlist stop/target/ladder exactly matching the reported effective state.

- [ ] **Step 1: Write failing persistence tests**

Add tests for preserved and explicit-clear behavior:

```python
def _add_position_with_two_level_plan():
    _add(ts_code="002050.SZ", shares=200)
    watchlist.update_plan("002050.SZ", {
        "doctrine": "single_v1",
        "scale_plan": [
            {"level": 1, "action": "trim", "trigger_price": 11.0, "pct": 1.0},
            {"level": 2, "action": "add", "trigger_price": 13.0,
             "shares": 100, "new_stop": 11.5},
        ],
    })
    return watchlist.load()["active_positions"][0]


def test_finalize_preserve_keeps_watchlist_and_records_effective_state(isolated_paths):
    baseline = _add_position_with_two_level_plan()
    proposal = {"action": "hold", "ladder_intent": "preserve", "rationale": "维持"}
    effective = materialize_effective_position_plan(baseline, proposal)
    entry = analyze._finalize_position_action(
        "002050.SZ", proposal, effective, "report", [], {}, {}, save=True,
    )
    position = watchlist.load()["active_positions"][0]
    assert position["stop_loss"] == effective["effective_stop"]
    assert position["target"] == effective["effective_target"]
    assert position["plan"]["scale_plan"] == effective["effective_scale_plan"]
    assert entry["position_action"]["new_stop"] is None
    assert entry["position_action"]["effective_stop"] == effective["effective_stop"]


def test_finalize_clear_removes_only_ladder(isolated_paths):
    baseline = _add_position_with_two_level_plan()
    proposal = {"action": "hold", "ladder_intent": "clear", "rationale": "取消条件单"}
    effective = materialize_effective_position_plan(baseline, proposal)
    analyze._finalize_position_action(
        "002050.SZ", proposal, effective, "report", [], {}, {}, save=True,
    )
    position = watchlist.load()["active_positions"][0]
    assert position["plan"]["scale_plan"] == []
    assert position["stop_loss"] == baseline["stop_loss"]
    assert position["target"] == baseline["target"]
```

Also assert a proposal that supplies a new stop updates the watchlist once, while a preserved effective stop is not misreported as a new recommendation.

- [ ] **Step 2: Run persistence tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_position_action_finalize.py`

Expected: failures because `_finalize_position_action` does not accept separate proposal/effective arguments or persist effective fields.

- [ ] **Step 3: Change finalization to consume both objects**

Change the signature to:

```python
def _finalize_position_action(
    ts_code: str,
    proposal: dict,
    effective: dict,
    analysis_text: str,
    events: list,
    playstyle_feats: dict,
    market_ctx: dict,
    save: bool = True,
    *,
    evidence_items: list | None = None,
    token_usage: dict | None = None,
) -> dict:
```

Persist legacy `new_stop/new_target` from the proposal, persist `scale_plan` from `effective_scale_plan`, and add `ladder_intent`, `effective_stop`, and `effective_target`. Update watchlist advice only when the proposal explicitly contains a new stop/target; always write the effective ladder. Keep the live “position still exists” race check before mutation.

- [ ] **Step 4: Wire graph completion to finalization**

At the end of `run()`, select both graph results:

```python
position_action_proposal = dict(graph_result.get("draft_proposal") or {})
position_action_effective = dict(graph_result.get("draft_data") or {})
```

Call `_finalize_position_action` only when `draft_kind == "position_action"` and both objects are non-empty. Do not reconstruct effective state after review/report.

- [ ] **Step 5: Run focused position-action suites**

Run: `.venv/bin/python -m pytest -q tests/test_position_action_state.py tests/test_position_action_dispatch.py tests/test_position_action_finalize.py tests/test_analysis_graph.py tests/test_ladder_path_sim.py tests/test_adherence.py`

Expected: all selected tests pass.

- [ ] **Step 6: Run complete verification**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests related to this change pass. If the two pre-existing sector-sentiment privacy-redaction tests still fail, record their exact names and counts without weakening assertions.

Run: `git diff --check c8e3181..HEAD`

Expected: no whitespace errors.

Run: `rg -n '\[DEBUG-' apex tests`

Expected: no temporary diagnostic instrumentation.

- [ ] **Step 7: Commit Task 4**

```bash
git add apex/analyze.py tests/test_position_action_finalize.py tests/test_analysis_graph.py
git commit -m "fix(analyze): 持久化持仓动作有效状态"
```

---

## Final Review Checklist

- [ ] Re-run the captured 000977 fixture and confirm no `新止损/新目标/条件触发计划与结构化结果不一致` failures.
- [ ] Confirm reviewer, report prompt, validator, journal entry, and watchlist all expose the same effective stop, target, and ladder.
- [ ] Confirm `preserve` never clears a plan and `clear` never preserves one.
- [ ] Confirm reviewer revision clears only the previous proposal/effective result, not the frozen baseline.
- [ ] Confirm old empty-plan payloads adapt to preserve and cannot delete active plans.
- [ ] Confirm unrelated user files remain untouched.
