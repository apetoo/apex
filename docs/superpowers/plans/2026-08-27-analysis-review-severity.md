# Analysis Review Severity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent minor reviewer comments from becoming `review_failure` while preserving abstention for repeated material conflicts and technical review failures.

**Architecture:** Normalize reviewer issues into a small internal severity model, then derive the state transition deterministically instead of trusting the model's `outcome`. Keep the public journal failure shape as strings and clarify in the reviewer prompt that a proposed position-plan change is not itself a conflict with the historical baseline.

**Tech Stack:** Python 3, LangGraph orchestration, pytest, OpenAI-compatible chat completion client.

**Spec:** `docs/superpowers/specs/2026-08-27-analysis-review-severity-design.md`

## Global Constraints

- Only material fact conflicts, direction-changing unsupported claims, critical unknowns, or technical review failures may end in `review_failure`.
- Minor issues receive at most one candidate revision and pass after the revision if no material issue remains.
- Existing journal/API output retains string failure messages.
- Existing report validation and evidence collection behavior must not change.

---

### Task 1: Deterministic review issue classification

**Files:**
- Modify: `apex/analyze.py:1599-1604`
- Test: `tests/test_analysis_graph.py`

**Interfaces:**
- Consumes: reviewer `outcome: str`, raw `issues: list[str | dict]`, and `revision_count: int`.
- Produces: `_normalize_review_issues(issues) -> list[dict]` and `_review_transition(outcome, issues, revision_count, technical_failure=False) -> tuple[str, list[str]]`.

- [ ] **Step 1: Write failing unit tests for minor and material transitions**

```python
def test_review_transition_downgrades_minor_abstain_after_revision_to_pass():
    issues = [{
        "message": "盘中站上均线但尚未收盘确认，作为 hold 支撑仍可接受",
        "severity": "minor",
        "blocking": False,
    }]
    assert analyze._review_transition("abstain", issues, revision_count=1) == (
        "pass", [issues[0]["message"]],
    )


def test_review_transition_revises_then_abstains_for_material_conflict():
    issues = [{
        "message": "候选方向与已确认财务事实存在重大冲突",
        "severity": "material",
        "blocking": True,
    }]
    assert analyze._review_transition("pass", issues, revision_count=0)[0] == "revise"
    assert analyze._review_transition("pass", issues, revision_count=1)[0] == "abstain"


def test_review_transition_keeps_technical_failure_blocking():
    outcome, messages = analyze._review_transition(
        "abstain", ["独立复核失败: invalid JSON"], revision_count=0,
        technical_failure=True,
    )
    assert outcome == "abstain"
    assert messages == ["独立复核失败: invalid JSON"]
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_analysis_graph.py -k 'review_transition'`

Expected: FAIL because `_review_transition` does not exist.

- [ ] **Step 3: Implement normalization and deterministic transition**

Add focused helpers near `_review_requires_revision`:

```python
_MATERIAL_REVIEW_MARKERS = (
    "重大冲突", "关键未知", "事实错误", "无支撑", "自相矛盾",
    "结论方向", "持续流出",
)


def _normalize_review_issues(issues: list) -> list[dict]:
    normalized = []
    for issue in issues or []:
        if isinstance(issue, dict):
            message = str(issue.get("message") or "").strip()
            severity = str(issue.get("severity") or "minor").lower()
            blocking = bool(issue.get("blocking")) or severity == "material"
        else:
            message = str(issue).strip()
            blocking = any(marker in message for marker in _MATERIAL_REVIEW_MARKERS)
            severity = "material" if blocking else "minor"
        if message:
            normalized.append({
                "message": message,
                "severity": severity if severity in {"minor", "material"} else "minor",
                "blocking": blocking,
            })
    return normalized


def _review_transition(outcome, issues, revision_count, *, technical_failure=False):
    normalized = _normalize_review_issues(issues)
    messages = [item["message"] for item in normalized]
    if technical_failure:
        return "abstain", messages
    material = any(item["blocking"] or item["severity"] == "material" for item in normalized)
    if material:
        return ("revise" if revision_count < 1 else "abstain"), messages
    if messages and revision_count < 1:
        return "revise", messages
    return "pass", messages
```

- [ ] **Step 4: Run unit tests and verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_analysis_graph.py -k 'review_transition'`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the classification seam**

```bash
git add apex/analyze.py tests/test_analysis_graph.py
git commit -m "fix(analyze): 分级处理独立复核问题"
```

---

### Task 2: Integrate severity transitions into the review node

**Files:**
- Modify: `apex/analyze.py:2275-2358`
- Test: `tests/test_analysis_graph.py`

**Interfaces:**
- Consumes: `_review_transition(...)` from Task 1.
- Produces: review events and LangGraph state with deterministic `pass|revise|abstain` transitions.

- [ ] **Step 1: Add an integration regression test for the 000977 failure pattern**

Construct a fake-client sequence with a held position candidate, first review response containing a minor issue and second review response returning `abstain` with only minor issues. Assert:

```python
assert result["analysis_status"] == "completed"
assert result["review_revision_count"] == 1
assert [event["outcome"] for event in events if event["type"] == "review"] == ["revise", "pass"]
```

The candidate must propose `new_stop=73.0` while `system_context` contains the old `71.5` ladder, with rationale explaining that MA60 has risen.

- [ ] **Step 2: Run the integration test and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_analysis_graph.py -k 'minor_reviewer_abstention_after_revision'`

Expected: FAIL with `analysis_status == "insufficient_evidence"` under the current review node.

- [ ] **Step 3: Update the reviewer contract and state transition**

Change the reviewer JSON contract to request structured issue objects and add this domain rule:

```text
system_context 中的 ladder/止损是历史基线；candidate 的 new_stop、new_target、scale_plan
是本次拟议修改。数值不同本身不是冲突；只有缺少调整依据、违反风险约束或候选内部互相矛盾时才记录问题。
```

After parsing the response, replace direct model-outcome handling with:

```python
technical_failure = payload is None
outcome, issues = _review_transition(
    outcome, issues, revision_count,
    technical_failure=technical_failure,
)
if outcome != "revise":
    outcome = controller.record_review(outcome, issues)
```

Keep emitted and persisted `issues` as strings.

- [ ] **Step 4: Run focused review tests**

Run: `.venv/bin/python -m pytest -q tests/test_analysis_graph.py -k 'review or material_review_issue or minor_reviewer_abstention'`

Expected: all selected tests pass, including existing material-conflict and provider-failure coverage.

- [ ] **Step 5: Commit the integration**

```bash
git add apex/analyze.py tests/test_analysis_graph.py
git commit -m "fix(analyze): 防止轻微复核问题误触发弃权"
```

---

### Task 3: Full verification

**Files:**
- Verify: `apex/analyze.py`
- Verify: `tests/test_analysis_graph.py`

**Interfaces:**
- Consumes: completed Tasks 1-2.
- Produces: evidence that the regression is fixed without weakening safety gates.

- [ ] **Step 1: Run the complete analysis graph suite**

Run: `.venv/bin/python -m pytest -q tests/test_analysis_graph.py`

Expected: zero failures.

- [ ] **Step 2: Run the complete Python suite**

Run: `.venv/bin/python -m pytest -q`

Expected: zero failures, except externally dependent tests explicitly identified as offline failures.

- [ ] **Step 3: Check formatting and unintended changes**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only intended implementation files and pre-existing untracked files remain.

- [ ] **Step 4: Review the final diff against the design**

Confirm that minor issues cannot directly cause `review_failure`, repeated material conflicts still do, technical failures still do, old ladder changes are described as proposals, and journal output remains backward compatible.
