# Authoritative Report Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the backend write the sole authoritative evidence-coverage field into verdict reports before strict validation.

**Architecture:** Add one deterministic, side-effect-free report normalizer beside the existing report parsing and validation helpers in `apex/analyze.py`. The report node will normalize only verdict reports after model generation and before `_validate_final_report`; all other report fields and validators remain unchanged.

**Tech Stack:** Python 3.12, pytest, LangGraph orchestration, OpenAI-compatible chat client

**Spec:** `docs/superpowers/specs/2026-09-01-authoritative-report-coverage-design.md`

## Global Constraints

- `candidate["evidence_coverage"]` is the sole source of truth.
- The published verdict report contains exactly one `**证据覆盖率：X%**` field.
- Do not change search, evidence scoring, verdict selection, or any non-coverage validator.
- Missing required report sections must still fail validation.
- Use test-first red-green-refactor for every production change.

---

### Task 1: Deterministic Coverage Normalizer

**Files:**
- Modify: `apex/analyze.py:1904-1940`
- Test: `tests/test_analysis_graph.py`

**Interfaces:**
- Consumes: `_normalize_report_evidence_coverage(text: str, candidate: dict) -> str` inputs a model-generated Markdown report and the finalized verdict candidate.
- Produces: Markdown with every model-authored bold evidence-coverage field removed and one authoritative field inserted immediately after `## 三、裁判结论` content begins; if the candidate has no coverage or the heading is absent, the input is returned unchanged.

- [ ] **Step 1: Write failing normalization tests**

Add focused tests covering omission, wrong value, and conflicting duplicates:

```python
@pytest.mark.parametrize(
    "model_fields",
    [
        "",
        "**证据覆盖率：10.0%**",
        "**证据覆盖率：10.0%**\n**证据覆盖率：90.0%**",
    ],
)
def test_normalize_report_evidence_coverage_writes_one_authoritative_field(model_fields):
    report = _complete_verdict_report().replace(
        "**证据覆盖率：60.0%**", model_fields,
    )

    normalized = analyze._normalize_report_evidence_coverage(
        report, {"evidence_coverage": 0.625},
    )

    assert normalized.count("**证据覆盖率：62.5%**") == 1
    assert len(analyze._report_labeled_values(normalized, "证据覆盖率")) == 1


def test_normalize_report_evidence_coverage_does_not_hide_missing_heading():
    report = _complete_verdict_report().replace("## 三、裁判结论", "## 裁判")

    assert analyze._normalize_report_evidence_coverage(
        report, {"evidence_coverage": 0.625},
    ) == report
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_analysis_graph.py -k normalize_report_evidence_coverage
```

Expected: FAIL because `apex.analyze` has no `_normalize_report_evidence_coverage` helper.

- [ ] **Step 3: Implement the minimal normalizer**

Add beside `_validate_labeled_number`:

```python
def _normalize_report_evidence_coverage(text: str, candidate: dict) -> str:
    coverage = candidate.get("evidence_coverage")
    heading = "## 三、裁判结论"
    if coverage is None or heading not in text:
        return text
    authoritative = f"**证据覆盖率：{round(float(coverage) * 100, 1)}%**"
    without_model_fields = re.sub(
        r"(?m)^\s*\*\*证据覆盖率：[^*\n]+\*\*\s*\n?",
        "",
        text,
    )
    section_start = without_model_fields.index(heading) + len(heading)
    return (
        without_model_fields[:section_start]
        + "\n"
        + authoritative
        + without_model_fields[section_start:]
    )
```

- [ ] **Step 4: Run focused tests and existing validator tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_analysis_graph.py -k 'normalize_report_evidence_coverage or final_report_validator'
```

Expected: PASS, including existing strict conflict tests.

- [ ] **Step 5: Commit Task 1**

```bash
git add apex/analyze.py tests/test_analysis_graph.py
git commit -m "fix(analyze): 规范化报告证据覆盖率"
```

---

### Task 2: Normalize Before Report Validation

**Files:**
- Modify: `apex/analyze.py:2915-2960`
- Test: `tests/test_analysis_graph.py`

**Interfaces:**
- Consumes: `_normalize_report_evidence_coverage(text, candidate)` from Task 1.
- Produces: the `report` graph node validates and persists the normalized verdict report while leaving position-action reports untouched.

- [ ] **Step 1: Write a failing report-node regression test**

Add a graph-level test using the existing fake chat response helpers. Configure the report response to contain two wrong fields:

```python
bad_report = _complete_verdict_report(verdict="中性", confidence=5).replace(
    "**证据覆盖率：60.0%**",
    "**证据覆盖率：10.0%**\n**证据覆盖率：90.0%**",
)
```

Run the existing analysis graph harness with a finalized candidate whose `evidence_coverage` is `0.6`, then assert:

```python
assert result["analysis_status"] == "completed"
assert result["analysis_text"].count("**证据覆盖率：60.0%**") == 1
assert "**证据覆盖率：10.0%**" not in result["analysis_text"]
assert "**证据覆盖率：90.0%**" not in result["analysis_text"]
```

- [ ] **Step 2: Run the regression test and verify RED**

Run the single new pytest node by its exact test name.

Expected: FAIL with `analysis_status == "insufficient_evidence"` and report-validation conflicts.

- [ ] **Step 3: Wire normalization into the report node**

Immediately after reading `choice.message.content` and before `_validate_final_report`, normalize only verdict reports:

```python
text = str(choice.message.content or "").strip()
if kind == "verdict":
    text = _normalize_report_evidence_coverage(text, candidate)
```

Change the verdict report prompt contract so the model is told the field is system-managed:

```python
"裁判结论中的证据覆盖率由系统写入；不要自行输出或计算证据覆盖率。"
"裁判结论必须逐字写出 `**净硬度：<net_hardness>**`。"
```

- [ ] **Step 4: Run graph regression and analysis tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_analysis_graph.py
```

Expected: PASS.

- [ ] **Step 5: Run the full Python suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS. If external-data tests fail offline, record their exact names and errors without weakening assertions.

- [ ] **Step 6: Inspect the final diff and commit Task 2**

Run:

```bash
git diff --check
git diff -- apex/analyze.py tests/test_analysis_graph.py
```

Then commit:

```bash
git add apex/analyze.py tests/test_analysis_graph.py
git commit -m "fix(analyze): 后端权威写入报告覆盖率"
```
