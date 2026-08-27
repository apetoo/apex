# Position Report Ladder Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a full-exit ladder with `new_stop<=0` renderable and verifiable without requiring the meaningless phrase “新止损 0”.

**Architecture:** Normalize only full-exit trim levels before report generation and deterministic comparison. Keep the standard machine-readable bullet grammar, but make the trailing new-stop clause conditional and include expected/parsed ladder summaries when validation fails.

**Tech Stack:** Python 3, pytest, LangGraph analysis orchestration.

**Spec:** `docs/superpowers/specs/2026-08-27-position-report-ladder-normalization-design.md`

## Global Constraints

- Only `trim` levels with `pct=1.0` and `new_stop<=0` normalize to `new_stop=None`.
- Positive new stops and partial trims remain strict.
- Natural-language ladders remain invalid.
- Evidence, review, trade-plan business validation, and journal schemas do not change.

---

### Task 1: Normalize and diagnose report ladder comparison

**Files:**
- Modify: `apex/analyze.py:1782-1837, 2416-2436`
- Test: `tests/test_analysis_graph.py:275-377`

**Interfaces:**
- Produces: `_normalize_report_scale_plan(candidate: dict) -> dict`, returning a copied candidate whose qualifying full-exit levels use `new_stop=None`.
- Consumes: normalized candidate in `report()` before prompt construction and `_validate_final_report()` comparison.

- [ ] **Step 1: Add failing regression tests**

```python
def _position_report_with_plan(plan_text: str) -> str:
    return _complete_position_report().replace(
        "**当前动作：hold**，保持现有仓位并执行既定风险计划。",
        "**当前动作：hold**\n**新止损：71.5**\n**新目标：90**\n保持现有仓位。",
    ).replace(
        "价格满足计划条件后才执行未来动作，当前不提前交易。",
        plan_text,
    )


def test_final_report_validator_allows_full_exit_to_omit_zero_new_stop():
    candidate = {
        "action": "hold", "new_stop": 71.5, "new_target": 90,
        "scale_plan": [
            {"action": "trim", "trigger_price": 71.5, "pct": 1.0, "new_stop": 0},
            {"action": "add", "trigger_price": 79.0, "shares": 100, "new_stop": 73.0},
        ],
    }
    report = _position_report_with_plan(
        "- trim @ 71.5，比例 1.0\n- add @ 79.0，100 股，新止损 73.0"
    )
    assert analyze._validate_final_report(
        report, "position_action", analyze._normalize_report_scale_plan(candidate),
    ) == []


def test_report_scale_plan_normalization_preserves_partial_trim_and_positive_stop():
    candidate = {"scale_plan": [
        {"action": "trim", "trigger_price": 80, "pct": 0.5, "new_stop": 0},
        {"action": "trim", "trigger_price": 71.5, "pct": 1.0, "new_stop": 70},
    ]}
    assert analyze._normalize_report_scale_plan(candidate) == candidate


def test_ladder_mismatch_reports_expected_and_parsed_values():
    candidate = {
        "action": "hold", "new_stop": 71.5, "new_target": 90,
        "scale_plan": [{"action": "add", "trigger_price": 79.0, "shares": 100}],
    }
    report = _position_report_with_plan("- add @ 80.0，100 股")
    issues = analyze._validate_final_report(report, "position_action", candidate)
    assert any("expected=" in issue and "parsed=" in issue for issue in issues)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_analysis_graph.py -k 'full_exit_to_omit_zero or report_scale_plan_normalization or ladder_mismatch_reports'`

Expected: FAIL because `_normalize_report_scale_plan` is absent and mismatch diagnostics are generic.

- [ ] **Step 3: Implement minimal normalization and diagnostics**

```python
def _normalize_report_scale_plan(candidate: dict) -> dict:
    normalized = dict(candidate or {})
    plan = []
    for raw_level in normalized.get("scale_plan") or []:
        level = dict(raw_level) if isinstance(raw_level, dict) else raw_level
        if (
            isinstance(level, dict)
            and level.get("action") == "trim"
            and float(level.get("pct") or 0) == 1.0
            and level.get("new_stop") is not None
            and float(level["new_stop"]) <= 0
        ):
            level["new_stop"] = None
        plan.append(level)
    normalized["scale_plan"] = plan
    return normalized
```

Call this helper in `report()` for `position_action` before building `authoritative_context`. Update the format contract so `，新止损 <new_stop>` is required only when the level's normalized `new_stop` is non-null. On ladder mismatch append:

```python
issues.append(
    "条件触发计划与结构化结果不一致："
    f"expected={expected_plan!r}; parsed={parsed_plan!r}"
)
```

- [ ] **Step 4: Verify GREEN and regression safety**

Run: `.venv/bin/python -m pytest -q tests/test_analysis_graph.py`

Expected: all analysis graph tests pass, including existing strict mismatch cases.

- [ ] **Step 5: Commit**

```bash
git add apex/analyze.py tests/test_analysis_graph.py
git commit -m "fix(analyze): 规范化全部退出档报告字段"
```

---

### Task 2: Final verification

**Files:**
- Verify: `apex/analyze.py`
- Verify: `tests/test_analysis_graph.py`

**Interfaces:**
- Consumes: Task 1 implementation.
- Produces: final test and diff evidence.

- [ ] **Step 1: Run focused and complete analysis tests**

Run: `.venv/bin/python -m pytest -q tests/test_analysis_graph.py`

Expected: zero failures.

- [ ] **Step 2: Run complete Python suite**

Run: `.venv/bin/python -m pytest -q`

Expected: document the two known unrelated privacy-redaction failures if still present; no new failures may involve `apex/analyze.py` or `tests/test_analysis_graph.py`.

- [ ] **Step 3: Inspect final diff**

Run: `git diff --check && git status --short`

Expected: no whitespace errors and no unintended tracked changes.
