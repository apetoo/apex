# Adaptive Detailed Analysis Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enrich verdict formal reports with adaptive, detailed market, history, playstyle, evidence-selection, and unknown-data explanations without weakening any accuracy gate.

**Architecture:** Create a focused `apex/report_context.py` boundary that converts structured runtime data into a small whitelisted report context. Pass that context explicitly into the existing LangGraph report node, then expand only the verdict report contract and validator; position-action reports and all decision/evidence algorithms remain unchanged.

**Tech Stack:** Python 3.12, pytest, LangGraph, OpenAI-compatible chat client

**Spec:** `docs/superpowers/specs/2026-09-01-adaptive-detailed-analysis-report-design.md`

## Global Constraints

- Final direction remains owned by the deterministic backend decision policy.
- `confirmed_evidence` remains the only source for formal bull/bear argument IDs.
- Coverage, net hardness, confidence, evidence IDs, and trade-plan validators remain strict.
- Research trace, assistant draft text, and uncounted facts must never enter the authoritative report context.
- Excluded claims may explain exclusion only and must never become directional evidence.
- Missing data is rendered as an explicit empty/unknown state; the model must not infer replacements.
- Position-action report format and behavior remain unchanged.
- Frontend files are out of scope because `VerdictDetailCard` already renders full `analysis_text` Markdown.

---

### Task 1: Whitelisted Authoritative Report Context

**Files:**
- Create: `apex/report_context.py`
- Create: `tests/test_report_context.py`

**Interfaces:**
- Produces: `build_report_context(*, history_entries: list[dict], market_context: dict, playstyle: dict | None, playstyle_features: dict, playstyle_fit: dict | None, risk_level: str | None, decision_policy: dict, unknowns: list[str]) -> dict`
- Produces keys: `market`, `history`, `playstyle`, `evidence_selection`, `unknowns`; each value is JSON-serializable and length-bounded.
- Consumes no trace events, prompts, assistant messages, or raw tool output.

- [ ] **Step 1: Write failing whitelist tests**

Create `tests/test_report_context.py` with fixtures modelled on 600487 and assertions that allowed fields survive while draft-only secrets do not:

```python
from apex.report_context import build_report_context


def _context():
    return build_report_context(
        history_entries=[{
            "analyzed_at": "2026-08-30T10:00:00+08:00",
            "verdict": "偏空", "confidence": 4,
            "calibrated_confidence": 3,
            "forecast_outcome": {"return_pct": 19.2, "hit": False},
            "analysis_text": "研究草稿中的 PEG=0.43 不得进入",
            "evidence": [{"fact": "未确认现金流 -8.65 亿"}],
        }],
        market_context={
            "as_of": "20260831",
            "stock_relative": {"chg_5d_pct": 11.5, "chg_20d_pct": 42.24,
                               "vs_index_5d_pct": 10.14, "ref_index_name": "沪深300"},
            "market_sentiment": {"regime": "亢奋", "market_style": "高潮",
                                 "total_score": 81, "reasons": ["涨停83家/跌停0家"],
                                 "private_raw": "drop"},
            "intraday": {"is_intraday": False, "shape": "高开低走（冲高回落）",
                         "day_chg_pct": -0.06, "vwap_position_pct": 0.67,
                         "raw_bars": [1, 2, 3]},
            "raw_response": "drop",
        },
        playstyle={"primary": "波段", "secondary": "中线", "ratings": {"波段": 4},
                   "reasons": ["20日波动率82.31%"], "method": "ai_skill"},
        playstyle_features={"completeness": 1.0, "risk_level": "high", "features": {
            "volatility": {"vol_20d_pct": 82.31, "present": True},
            "valuation": {"pe_ttm": 39.69, "present": True},
        }, "notes": ["资金日部分缺失"]},
        playstyle_fit={"state": "insufficient_data", "note": "画像累积中"},
        risk_level="high",
        decision_policy={
            "counted_claims": [{"evidence_id": "sys_capital", "dimension": "capital",
                                "stance": "bear", "inference": "近5日资金流出"}],
            "excluded_claims": [{"evidence_id": "ev_old", "reason": "stale_capital",
                                 "inference": "旧龙虎榜资金"}],
        },
        unknowns=["现金流口径未确认"],
    )


def test_build_report_context_keeps_only_whitelisted_authority():
    context = _context()
    rendered = repr(context)
    assert context["market"]["sentiment"]["regime"] == "亢奋"
    assert context["history"][0]["verdict"] == "偏空"
    assert context["playstyle"]["profile"]["primary"] == "波段"
    assert context["evidence_selection"]["excluded"][0]["reason"] == "stale_capital"
    assert context["unknowns"] == ["现金流口径未确认"]
    assert "analysis_text" not in rendered
    assert "PEG=0.43" not in rendered
    assert "未确认现金流" not in rendered
    assert "raw_bars" not in rendered
    assert "raw_response" not in rendered
    assert "private_raw" not in rendered


def test_build_report_context_returns_explicit_empty_states():
    context = build_report_context(
        history_entries=[], market_context={}, playstyle=None, playstyle_features={},
        playstyle_fit=None, risk_level=None, decision_policy={}, unknowns=[],
    )
    assert context == {
        "market": {}, "history": [], "playstyle": {},
        "evidence_selection": {"counted": [], "excluded": []}, "unknowns": [],
    }
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_report_context.py
```

Expected: collection fails because `apex.report_context` does not exist.

- [ ] **Step 3: Implement the minimal whitelist module**

Implement explicit field selection, capped history and list lengths, and JSON-safe scalar copying. Use these limits:

```python
MAX_HISTORY = 5
MAX_REASONS = 5
MAX_UNKNOWNS = 8
MAX_TEXT = 240


def _text(value) -> str:
    return str(value or "")[:MAX_TEXT]


def _pick(source: dict, keys: tuple[str, ...]) -> dict:
    return {key: source[key] for key in keys if source.get(key) is not None}
```

Whitelisted fields:

- history: `analyzed_at`, `date`, `verdict`, `confidence`, `calibrated_confidence`, `forecast_outcome`
- sentiment: `as_of`, `regime`, `market_style`, `total_score`, `reasons`
- stock relative: `chg_5d_pct`, `chg_20d_pct`, `vs_index_5d_pct`, `ref_index_name`
- intraday: `trade_date`, `as_of_time`, `is_intraday`, `day_chg_pct`, `amplitude_pct`, `vol_ratio`, `vol_label`, `shape`, `vwap_position_pct`
- playstyle: nested `profile` with `primary`, `secondary`, `ratings`, `reasons`, `method`, `low_confidence`; plus final `risk_level`, `fit`, and whitelisted `features`
- playstyle features: only named feature groups already present under `features`; within each group retain scalar values and discard nested raw arrays/objects
- claims: `evidence_id`, `stance`, `dimension`, `nature`, `hardness`, `adjusted_hardness`, `as_of`, `frequency`, `inference`, plus `reason` for excluded claims

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_report_context.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add apex/report_context.py tests/test_report_context.py
git commit -m "feat(analyze): 构建正式报告权威上下文"
```

---

### Task 2: Pass Structured Runtime Context Into the Report Node

**Files:**
- Modify: `apex/analyze.py:2276-2290`
- Modify: `apex/analyze.py:2879-2945`
- Modify: `apex/analyze.py:3060-3210`
- Test: `tests/test_analysis_graph.py`

**Interfaces:**
- Consumes Task 1 `build_report_context(...)`.
- Adds `_run_langgraph_loop(..., report_context: dict | None = None)`; default `{}` preserves direct test callers.
- `run()` creates the base structured report context from `history_entries`, `market_ctx`, and `playstyle_feats` instead of passing new information through `system_context` text.
- The report node combines the base context with finalized `decision_policy`, current `unknowns`, and backend-finalized playstyle.

- [ ] **Step 1: Write a failing report-prompt boundary test**

Extend `tests/test_analysis_graph.py` with a test that calls `_run_langgraph_loop` using `report_context` containing a safe market/history marker and messages containing a draft-only marker:

```python
def test_formal_report_prompt_uses_structured_authority_not_research_draft(monkeypatch):
    _patch_report_graph_environment(monkeypatch)
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "观望偏空", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=_bearish_candidate()),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=_complete_adaptive_verdict_report()),
    ])

    result = analyze._run_langgraph_loop(
        ts_code="002192.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "DRAFT_ONLY_PEG_0_43"}],
        max_iter=12, emit=lambda _event: None,
        report_context={
            "market": {"sentiment": {"regime": "亢奋"}},
            "history": [{"verdict": "偏空", "forecast_outcome": {"hit": False}}],
            "playstyle": {},
            "evidence_selection": {"counted": [], "excluded": []},
            "unknowns": [],
        },
    )

    assert result["analysis_status"] == "completed"
    prompt = client.calls[-1]["messages"][-1]["content"]
    assert '"regime": "亢奋"' in prompt
    assert '"hit": false' in prompt
    assert "DRAFT_ONLY_PEG_0_43" not in prompt
```

- [ ] **Step 2: Run the new test and verify RED**

Run the exact pytest node. Expected: FAIL because `_run_langgraph_loop` does not accept `report_context`.

- [ ] **Step 3: Wire the structured context**

Implement:

```python
def _run_langgraph_loop(..., report_context: dict | None = None, ...) -> dict:
    authoritative_report_context = deepcopy(report_context or {})
```

In `run()`, call `build_report_context` with history/market/playstyle feature inputs, `playstyle=None`, empty decision policy/unknowns, then pass it as `report_context`. This creates a sanitized base containing market/history and the whitelisted playstyle features but no unfinalized profile.

In the verdict report node, rebuild or merge the context after `prepare_candidate` has supplied final `decision_policy`:

```python
adaptive_context = deepcopy(authoritative_report_context)
adaptive_context["evidence_selection"] = {
    "counted": list(decision_policy_context.get("counted_claims") or []),
    "excluded": list(decision_policy_context.get("excluded_claims") or []),
}
adaptive_context["unknowns"] = list(state.get("unknowns") or [])
authoritative_context["adaptive_report"] = adaptive_context
```

Use the existing `finalize_candidate` callback seam to finalize playstyle only after the verdict candidate exists:

```python
def _finalize_report_candidate(kind: str, candidate: dict) -> tuple[dict, dict]:
    if kind != "verdict":
        return dict(candidate), {}
    report_playstyle = playstyle.finalize_playstyle(candidate.get("playstyle"), playstyle_feats)
    return dict(candidate), {
        "playstyle": report_playstyle,
        "playstyle_fit": playstyle.compute_playstyle_fit(report_playstyle),
        "risk_level": playstyle_feats.get("risk_level"),
    }
```

Pass `_finalize_report_candidate` as `finalize_candidate`. In the report node, copy its finalized metadata into the already-sanitized base:

```python
adaptive_context["playstyle"]["profile"] = dict(
    finalization_metadata.get("playstyle") or {}
)
adaptive_context["playstyle"]["fit"] = dict(
    finalization_metadata.get("playstyle_fit") or {}
)
adaptive_context["playstyle"]["risk_level"] = finalization_metadata.get("risk_level")
```

Do not expose raw candidate messages. The journal persistence path may continue calling the same deterministic playstyle functions; tests must assert its stored result equals the report metadata result.

- [ ] **Step 4: Add `run()` propagation assertions**

Extend the existing `test_run_passes_distinct_proposal_effective_state_and_frozen_baseline_to_finalization` spy or add a verdict-specific spy asserting `kwargs["report_context"]` contains only `market`, `history`, `playstyle`, `evidence_selection`, and `unknowns`, and that no history `analysis_text` is present.

- [ ] **Step 5: Run report graph tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_report_context.py tests/test_analysis_graph.py -k 'report_context or formal_report or run_passes'
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add apex/analyze.py tests/test_analysis_graph.py
git commit -m "feat(analyze): 注入正式报告结构化上下文"
```

---

### Task 3: Adaptive Detailed Verdict Contract and Strict Validation

**Files:**
- Modify: `apex/analyze.py:1868-2050`
- Modify: `apex/analyze.py:2830-2965`
- Test: `tests/test_analysis_graph.py`

**Interfaces:**
- Expands `_VERDICT_REPORT_SECTIONS` with the required adaptive detailed headings.
- Keeps `_POSITION_REPORT_SECTIONS` unchanged.
- Produces a formal report prompt that explicitly distinguishes directional evidence, background context, excluded claims, and unknowns.

- [ ] **Step 1: Add the adaptive report fixture and failing validator test**

Create `_complete_adaptive_verdict_report()` from the existing `_complete_verdict_report()` and require these exact headings:

```python
(
    "## 核心判断",
    "## 基本面分析",
    "## 市场与个股环境",
    "## 四维分析",
    "## 一、多头论点",
    "## 二、空头论点",
    "## 三、裁判结论",
    "### 加权四维评分",
    "### 证据取舍与冲突",
    "### 历史判断复盘",
    "### 玩法与适用周期",
    "### 置信度调整",
    "## 操作建议",
    "## 风险与未知项",
)
```

Tests:

```python
def test_adaptive_verdict_report_requires_detailed_sections():
    report = _complete_adaptive_verdict_report()
    assert analyze._validate_final_report(report, "verdict", {
        "verdict": "观望偏空", "confidence": 4,
    }) == []
    issues = analyze._validate_final_report(
        report.replace("## 市场与个股环境", "## 市场"), "verdict",
        {"verdict": "观望偏空", "confidence": 4},
    )
    assert any("市场与个股环境" in issue for issue in issues)


def test_position_report_sections_remain_unchanged():
    assert "## 市场与个股环境" not in analyze._POSITION_REPORT_SECTIONS
    assert analyze._validate_final_report(
        _complete_position_report(), "position_action", {"action": "hold"},
    ) == []
```

- [ ] **Step 2: Run validator tests and verify RED**

Run the two exact pytest nodes. Expected: the missing adaptive section is not rejected yet.

- [ ] **Step 3: Expand only the verdict contract**

Update `_VERDICT_REPORT_SECTIONS` to the exact heading tuple above. Keep `_POSITION_REPORT_SECTIONS` byte-for-byte unchanged.

Change the verdict `format_contract` to state:

- `adaptive_report.market/history/playstyle/evidence_selection/unknowns` are the only sources for the new sections.
- `evidence_selection.counted` may be discussed as directional evidence.
- `evidence_selection.excluded` may only explain exclusion and must not appear as bull/bear evidence IDs.
- Missing arrays/objects must produce one short “无可靠数据/无历史样本” sentence.
- Do not copy or infer facts from prior assistant messages.
- Target 2000–3000 Chinese characters when authority exists; accuracy and completeness override length.

- [ ] **Step 4: Add a 600487-style prompt regression**

Use markers representing safe and unsafe facts:

```python
assert "亢奋" in report_prompt
assert "stale_capital" in report_prompt
assert "波段" in report_prompt
assert "历史样本" in report_prompt
assert "DRAFT_ONLY_CASHFLOW_MINUS_8_65" not in report_prompt
assert "被排除证据只能解释排除原因" in report_prompt
```

Also assert the final report includes all adaptive headings and preserves structured verdict, confidence, coverage, net hardness, and price advice.

- [ ] **Step 5: Run all analysis report tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_report_context.py tests/test_analysis_graph.py
```

Expected: all tests pass.

- [ ] **Step 6: Run the full Python suite and inspect the diff**

Run:

```bash
.venv/bin/python -m pytest -q
git diff --check
git diff -- apex/report_context.py apex/analyze.py tests/test_report_context.py tests/test_analysis_graph.py
```

Expected: report-context and analysis tests pass. If the two known sector-sentiment privacy tests still fail, verify them against the base revision and record them without weakening assertions.

- [ ] **Step 7: Commit Task 3**

```bash
git add apex/analyze.py tests/test_analysis_graph.py
git commit -m "feat(analyze): 生成自适应详细正式报告"
```
