import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from apex import analyze, observability
from apex.analysis_graph import GraphHandlers, build_analysis_graph


def _handlers(events, *, review_outcomes=None, force_abstain=False):
    reviews = iter(review_outcomes or ["pass"])

    def node(name, update):
        def run(state):
            events.append(name)
            value = update(state) if callable(update) else update
            return value
        return run

    return GraphHandlers(
        prepare=node("prepare", {}),
        safety_scan=node("safety_scan", {"safety_scan_status": "clear"}),
        reason=node("reason", lambda state: {
            "pending_tools": ["web_search"] if state.get("research_rounds", 0) == 0 else [],
        }),
        execute_tools=node("tools", {"research_rounds": 1, "new_evidence_count": 1, "pending_tools": []}),
        assess=node("assess", {"route": "abstain" if force_abstain else "draft"}),
        draft=node("draft", {"draft": {"verdict": "中性"}}),
        review=node("review", lambda state: {"review_outcome": next(reviews)}),
        report=node("report", {"report_route": "finalize", "analysis_text": "完整报告"}),
        finalize=node("finalize", {"analysis_status": "completed"}),
        abstain=node("abstain", {"analysis_status": "insufficient_evidence"}),
    )


def test_graph_routes_research_through_tools_and_review_to_finalize():
    events = []
    graph = build_analysis_graph(_handlers(events))

    result = graph.invoke({"research_rounds": 0}, {"recursion_limit": 30})

    assert result["analysis_status"] == "completed"
    assert events == [
        "prepare", "safety_scan", "reason", "tools", "assess",
        "draft", "review", "report", "finalize",
    ]


def test_graph_can_abstain_from_assessment():
    events = []
    graph = build_analysis_graph(_handlers(events, force_abstain=True))

    result = graph.invoke({"research_rounds": 0}, {"recursion_limit": 30})

    assert result["analysis_status"] == "insufficient_evidence"
    assert events[-1] == "abstain"
    assert "draft" not in events


def test_review_rework_returns_to_reason_once_then_passes():
    events = []
    handlers = _handlers(events, review_outcomes=["rework", "pass"])
    original_reason = handlers.reason

    def reason(state):
        update = original_reason(state)
        if state.get("review_outcome") == "rework":
            return {"pending_tools": [], "review_outcome": None}
        return update

    handlers.reason = reason
    graph = build_analysis_graph(handlers)

    result = graph.invoke({"research_rounds": 0}, {"recursion_limit": 30})

    assert result["analysis_status"] == "completed"
    assert events.count("review") == 2
    assert events.count("reason") == 2


def test_review_can_abstain_without_finalizing():
    events = []
    graph = build_analysis_graph(_handlers(events, review_outcomes=["abstain"]))

    result = graph.invoke({"research_rounds": 0}, {"recursion_limit": 30})

    assert result["analysis_status"] == "insufficient_evidence"
    assert events[-1] == "abstain"
    assert "finalize" not in events


def _response(*, tool_name=None, arguments=None, content=None):
    tool_calls = []
    finish_reason = "stop"
    if tool_name:
        finish_reason = "tool_calls"
        tool_calls = [SimpleNamespace(
            id=f"call-{tool_name}",
            function=SimpleNamespace(name=tool_name, arguments=json.dumps(arguments or {}, ensure_ascii=False)),
        )]
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


class _FakeClient:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self._responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_accepted_position_action_stores_adapted_proposal_and_effective_draft(monkeypatch):
    baseline = {
        "ts_code": "000977.SZ", "entry_price": 77.095,
        "position_size_shares": 200, "stop_loss": 71.5, "target": 90.0,
        "plan": {"scale_plan": [
            {"level": 1, "action": "trim", "trigger_price": 71.5, "pct": 1.0},
            {"level": 2, "action": "add", "trigger_price": 79.0, "shares": 100,
             "new_stop": 73.0},
        ]},
    }
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "维持持仓", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_position_action", arguments={
            "action": "hold", "scale_plan": [], "rationale": "维持原计划",
        }),
        _response(content=json.dumps({"outcome": "abstain", "issues": []}, ensure_ascii=False)),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({
        "results": [{
            "title": "浪潮信息公告", "snippet": "未见新增重大风险", "url": "https://www.cninfo.com.cn/scan",
            "date": "2026-08-25", "site": "巨潮资讯", "source_tier": 1,
            "entity_matched": True, "freshness_status": "current",
        }],
    }))
    monkeypatch.setattr(analyze.data, "get_realtime_price", lambda _codes: {"000977.SZ": 78.27})
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": [baseline]})

    result = analyze._run_langgraph_loop(
        ts_code="000977.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12,
        emit=lambda _event: None, position_baseline=baseline,
    )

    assert result["draft_proposal"]["ladder_intent"] == "preserve"
    assert result["draft_data"]["effective_stop"] == 71.5
    assert result["draft_data"]["effective_target"] == 90.0
    assert len(result["draft_data"]["effective_scale_plan"]) == 2


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
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": [{
        "title": "浪潮信息公告", "snippet": "未见新增重大风险", "url": "https://www.cninfo.com.cn/scan",
        "date": "2026-08-25", "site": "巨潮资讯", "source_tier": 1,
        "entity_matched": True, "freshness_status": "current",
    }]}))
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
    review_prompt = next(
        call["messages"][-1]["content"]
        for call in client.calls
        if call["messages"] and isinstance(call["messages"][0], dict)
        and call["messages"][0]["role"] == "system"
        and "独立审稿人" in call["messages"][0]["content"]
    )
    assert '"baseline"' in review_prompt
    assert '"proposal"' in review_prompt
    assert '"effective"' in review_prompt
    report_prompt = client.calls[-1]["messages"][-1]["content"]
    assert "effective 是不可修改的最终机器结果" in report_prompt


def test_held_position_prompt_requires_explicit_preserve_replace_or_clear_intent():
    prompt = analyze._format_held_ladder_block({
        "ts_code": "000977.SZ", "entry_price": 77.095,
        "position_size_shares": 200, "stop_loss": 71.5, "target": 90.0,
        "plan": {"scale_plan": [{"level": 1, "action": "trim", "trigger_price": 71.5, "pct": 1.0}]},
    }, "000977.SZ")

    assert "ladder_intent 必填" in prompt
    assert "preserve：不提交 scale_plan" in prompt
    assert "replace：提交完整的期望 ladder" in prompt
    assert "clear：明确清空 ladder，不提交 scale_plan" in prompt
    assert "演进，非替换" not in prompt


def test_empty_position_ladder_prompt_and_schema_require_explicit_intent():
    prompt = analyze._format_held_ladder_block({
        "ts_code": "000977.SZ", "entry_price": 77.095,
        "position_size_shares": 200, "stop_loss": 71.5, "target": 90.0,
        "plan": {"scale_plan": []},
    }, "000977.SZ")
    schema = next(tool["function"] for tool in analyze.TOOLS
                  if tool["function"]["name"] == "record_position_action")["parameters"]

    assert "ladder_intent 必填" in prompt
    assert "replace：提交完整的非空 scale_plan" in prompt
    assert "preserve：保持空 ladder，不提交 scale_plan" in prompt
    assert schema["properties"]["ladder_intent"]["enum"] == ["preserve", "replace", "clear"]
    assert "ladder_intent" in schema["required"]
    assert set(schema["properties"]["scale_plan"]["items"]["required"]) == {
        "action", "trigger_price",
    }
    assert len(schema["properties"]["scale_plan"]["items"]["oneOf"]) == 2
    assert schema["allOf"][0]["then"]["required"] == ["scale_plan"]


def _multi_tool_response(calls):
    tool_calls = [SimpleNamespace(
        id=f"call-{index}-{name}",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    ) for index, (name, arguments) in enumerate(calls)]
    message = SimpleNamespace(content=None, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")])


def _complete_verdict_report(*, verdict="观望偏空", confidence=4):
    return f"""## 核心判断
**判断：{verdict}**
**置信度：{confidence}/10**

## 基本面分析
营收与利润数据完整，周期位置仍需谨慎。

## 一、多头论点
1. 盈利增长 → 提供安全边际。
2. 现金流改善 → 盈利质量提升。
3. 负债可控 → 财务风险有限。

## 二、空头论点
1. 商品价格回落 → 利润可能承压。
2. 资金净流出 → 短线承接偏弱。
3. 趋势未反转 → 当前不宜追高。

## 三、裁判结论
领先指标弱于滞后的利润数据，因此倾向谨慎。
**证据覆盖率：60.0%**

### 加权四维评分
技术面 4 分，基本面 7 分，资金面 3 分，情绪面 5 分；加权总分 4.9。

### 置信度调整
初始 6，历史命中率扣 2，最终 {confidence}。

## 操作建议
当前不新开多头仓位，等待资金与趋势改善后重新评估。

## 风险提示
商品价格和市场波动可能使结论失效。"""


def _complete_position_report(*, action="hold"):
    return f"""## 核心判断
**当前动作：{action}**

## 基本面分析
盈利与现金流保持稳定，资产负债表未出现新增重大风险。

## 一、多头论点
1. 盈利增长 → 支撑继续持有。
2. 现金流改善 → 降低经营风险。
3. 估值合理 → 保留现有仓位。

## 二、空头论点
1. 趋势偏弱 → 当前不宜加仓。
2. 资金流出 → 短线承接不足。
3. 波动较高 → 必须严格执行止损。

## 三、裁判结论
基本面仍可，但技术与资金尚未支持加仓，因此维持当前动作。

### 加权四维评分
技术面 4 分、基本面 7 分、资金面 4 分、情绪面 5 分，加权总分 5.2。

## 当前持仓动作
**当前动作：{action}**，保持现有仓位并执行既定风险计划。

## 条件触发计划
价格满足计划条件后才执行未来动作，当前不提前交易。

## 风险提示
趋势进一步转弱或基本面恶化可能触发止损。"""


def _position_report_with_plan(plan_text: str) -> str:
    return _complete_position_report().replace(
        "**当前动作：hold**，保持现有仓位并执行既定风险计划。",
        "**当前动作：hold**\n**当前有效止损：71.5**\n**当前有效目标：90**\n保持现有仓位。",
    ).replace(
        "价格满足计划条件后才执行未来动作，当前不提前交易。",
        plan_text,
    )


@pytest.mark.parametrize(
    ("model_fields", "remaining_prose"),
    [
        ("", ""),
        ("**证据覆盖率：10.0%**", ""),
        ("**证据覆盖率：10.0%**\n**证据覆盖率：90.0%**", ""),
        ("结论：**证据覆盖率：10.0%**，仍需等待确认。", "结论：仍需等待确认。"),
        (
            "结论：**证据覆盖率：10.0%**；仍需等待确认；**证据覆盖率：90.0%**",
            "结论：仍需等待确认；",
        ),
    ],
)
def test_normalize_report_evidence_coverage_writes_one_authoritative_field(
    model_fields, remaining_prose,
):
    report = _complete_verdict_report().replace(
        "**证据覆盖率：60.0%**", model_fields,
    )

    normalized = analyze._normalize_report_evidence_coverage(
        report, {"evidence_coverage": 0.625},
    )

    assert normalized.count("**证据覆盖率：62.5%**") == 1
    assert analyze._report_labeled_values(normalized, "证据覆盖率") == ["62.5%"]
    if remaining_prose:
        assert remaining_prose in normalized


def test_normalize_report_evidence_coverage_does_not_hide_missing_heading():
    report = _complete_verdict_report().replace("## 三、裁判结论", "## 裁判")

    assert analyze._normalize_report_evidence_coverage(
        report, {"evidence_coverage": 0.625},
    ) == report


def test_final_report_validator_rejects_missing_sections_and_process_text():
    issues = analyze._validate_final_report(
        "让我查询。\n## 核心判断\n**判断：观望偏空**\n**置信度：4/10**",
        "verdict",
        {"verdict": "观望偏空", "confidence": 4},
    )

    assert any("基本面分析" in issue for issue in issues)
    assert any("过程性措辞" in issue for issue in issues)


def test_final_report_validator_accepts_complete_report_and_rejects_field_conflicts():
    candidate = {"verdict": "观望偏空", "confidence": 4}

    assert analyze._validate_final_report(
        _complete_verdict_report(), "verdict", candidate,
    ) == []

    issues = analyze._validate_final_report(
        _complete_verdict_report(verdict="偏多", confidence=6), "verdict", candidate,
    )
    assert any("判断与结构化结果不一致" in issue for issue in issues)
    assert any("置信度与结构化结果不一致" in issue for issue in issues)

    conflicting = _complete_verdict_report() + "\n\n**判断：看多**\n**置信度：8/10**"
    issues = analyze._validate_final_report(conflicting, "verdict", candidate)
    assert any("存在冲突的判断" in issue for issue in issues)

    issues = analyze._validate_final_report(
        _complete_verdict_report() + "\n\n最终置信度：8/10", "verdict", candidate,
    )
    assert any("存在冲突的置信度" in issue for issue in issues)
    assert any("存在冲突的置信度" in issue for issue in issues)

    issues = analyze._validate_final_report(
        _complete_verdict_report() + "\n\n最终判断：看多", "verdict", candidate,
    )
    assert any("存在冲突的判断" in issue for issue in issues)


def test_final_report_validator_enforces_position_action_contract():
    report = """## 核心判断
**当前动作：hold**

## 基本面分析
盈利与现金流保持稳定，暂未发现资产负债表风险。

## 一、多头论点
1. 盈利增长 → 支撑持有。
2. 现金流改善 → 降低风险。
3. 估值合理 → 保留仓位。

## 二、空头论点
1. 趋势偏弱 → 不宜加仓。
2. 资金流出 → 承接不足。
3. 波动较高 → 严格止损。

## 三、裁判结论
基本面仍可，但技术和资金不支持加仓，因此维持持有。

### 加权四维评分
技术 4，基本 7，资金 4，情绪 5，加权总分 5.2。

## 当前持仓动作
**当前动作：hold**，保持现有仓位并执行既定止损。

## 条件触发计划
价格满足计划条件后再执行，当前不提前交易。

## 风险提示
趋势进一步转弱可能触发止损。"""
    candidate = {"action": "hold", "rationale": "保持仓位", "scale_plan": []}

    assert analyze._validate_final_report(report, "position_action", candidate) == []

    issues = analyze._validate_final_report(
        report.replace("**当前动作：hold**", "**当前动作：trim**"),
        "position_action", candidate,
    )
    assert any("当前动作与结构化结果不一致" in issue for issue in issues)


def test_final_report_validator_enforces_position_risk_and_ladder_fields():
    report = _complete_position_report().replace(
        "**当前动作：hold**，保持现有仓位并执行既定风险计划。",
        "**当前动作：hold**\n**当前有效止损：4.8**\n**当前有效目标：7.5**\n保持现有仓位。",
    ).replace(
        "价格满足计划条件后才执行未来动作，当前不提前交易。",
        "- add @ 5.2，100 股，新止损 4.9；价格触发后才执行。",
    )
    candidate = {
        "action": "hold", "effective_stop": 4.8, "effective_target": 7.5,
        "effective_scale_plan": [{
            "action": "add", "trigger_price": 5.2, "shares": 100,
            "new_stop": 4.9, "reason": "突破确认",
        }],
        "rationale": "保持仓位",
    }
    candidate = analyze._effective_position_report_candidate(candidate)

    assert analyze._validate_final_report(report, "position_action", candidate) == []

    issues = analyze._validate_final_report(
        report.replace("**当前有效止损：4.8**", "**当前有效止损：4.2**"),
        "position_action", candidate,
    )
    assert any("当前有效止损与结构化结果不一致" in issue for issue in issues)

    issues = analyze._validate_final_report(
        report + "\n**当前动作：add**", "position_action", candidate,
    )
    assert any("存在冲突的当前动作" in issue for issue in issues)

    issues = analyze._validate_final_report(
        report + "\n当前立即加仓500股。", "position_action", candidate,
    )
    assert any("当前指令与结构化动作不一致" in issue for issue in issues)

    issues = analyze._validate_final_report(
        report + "\n**当前有效目标：7.2**", "position_action", candidate,
    )
    assert any("当前有效目标与结构化结果不一致" in issue for issue in issues)


def test_final_report_validator_requires_explicit_clear_ladder_statement():
    """A clear effective state must report the removal, not silently omit its ladder."""
    candidate = analyze._effective_position_report_candidate({
        "action": "hold", "ladder_intent": "clear",
        "effective_stop": 71.5, "effective_target": 90.0,
        "effective_scale_plan": [],
    })
    report = _position_report_with_plan("条件触发计划已清空")

    assert analyze._validate_final_report(report, "position_action", candidate) == []
    assert any(
        "ladder_intent=clear" in issue
        for issue in analyze._validate_final_report(
            _position_report_with_plan("本次不设置条件触发计划。"),
            "position_action", candidate,
        )
    )


def test_final_report_validator_compares_each_ladder_level_in_order():
    candidate = {
        "action": "hold", "scale_plan": [
            {"action": "add", "trigger_price": 5.2, "shares": 100, "new_stop": 4.9},
            {"action": "trim", "trigger_price": 6.0, "shares": 200, "new_stop": 5.3},
        ],
    }
    report = _complete_position_report().replace(
        "价格满足计划条件后才执行未来动作，当前不提前交易。",
        "- add @ 5.2，200 股，新止损 4.9\n- trim @ 6.0，100 股，新止损 5.3",
    )

    issues = analyze._validate_final_report(report, "position_action", candidate)

    assert any("条件触发计划与结构化结果不一致" in issue for issue in issues)


def test_final_report_validator_enforces_bullish_trade_plan_fields():
    report = _complete_verdict_report(verdict="偏多", confidence=6).replace(
        "当前不新开多头仓位，等待资金与趋势改善后重新评估。",
        "**入场：10.5–11.0**\n**止损：9.8**\n**目标：13.0**\n**建议仓位：10%**",
    )
    candidate = {
        "verdict": "偏多", "confidence": 6,
        "entry": 10.8, "entry_low": 10.5, "entry_high": 11.0,
        "stop_loss": 9.8, "target": 13.0, "position_size_pct": 10,
    }

    assert analyze._validate_final_report(report, "verdict", candidate) == []

    issues = analyze._validate_final_report(
        report.replace("**止损：9.8**", "**止损：9.2**"), "verdict", candidate,
    )
    assert any("止损与结构化结果不一致" in issue for issue in issues)

    issues = analyze._validate_final_report(
        report + "\n**止损：9.2**", "verdict", candidate,
    )
    assert any("存在冲突的止损" in issue for issue in issues)

    issues = analyze._validate_final_report(
        report.replace("**止损：9.8**", "**止损：9.8 或 9.2**"), "verdict", candidate,
    )
    assert any("存在冲突的止损" in issue for issue in issues)


def test_final_report_validator_supports_pct_ladder_without_throwing():
    candidate = {
        "action": "hold", "scale_plan": [
            {"action": "trim", "trigger_price": 6.0, "pct": 0.25},
        ],
    }
    report = _complete_position_report().replace(
        "价格满足计划条件后才执行未来动作，当前不提前交易。",
        "- trim @ 6.0，比例 0.25",
    )

    assert analyze._validate_final_report(report, "position_action", candidate) == []

    malformed = {"action": "hold", "scale_plan": [None]}
    issues = analyze._validate_final_report(report, "position_action", malformed)
    assert any("结构化条件触发计划字段无效" in issue for issue in issues)


def test_final_report_validator_accepts_domain_normalized_full_exit_ladder():
    candidate = {
        "action": "hold", "effective_stop": 71.5, "effective_target": 90,
        "effective_scale_plan": [
            {"action": "trim", "trigger_price": 71.5, "pct": 1.0, "new_stop": None},
            {"action": "add", "trigger_price": 79.0, "shares": 100, "new_stop": 73.0},
        ],
    }
    report = _position_report_with_plan(
        "- trim @ 71.5，比例 1.0\n- add @ 79.0，100 股，新止损 73.0"
    )
    assert analyze._validate_final_report(
        report, "position_action", analyze._effective_position_report_candidate(candidate),
    ) == []


def test_exit_report_does_not_require_a_baseline_stop_or_target_label():
    candidate = analyze._effective_position_report_candidate({
        "action": "exit", "effective_stop": 71.5, "effective_target": 90.0,
        "effective_scale_plan": [], "ladder_intent": "preserve",
    })
    report = _complete_position_report(action="exit").replace(
        "价格满足计划条件后才执行未来动作，当前不提前交易。",
        "本次无后续条件触发计划。",
    )

    assert candidate["effective_stop"] == 71.5
    assert candidate["effective_target"] == 90.0
    assert analyze._validate_final_report(report, "position_action", candidate) == []


def test_run_passes_distinct_proposal_effective_state_and_frozen_baseline_to_finalization(monkeypatch):
    baseline = {
        "ts_code": "000977.SZ", "position_size_shares": 200,
        "stop_loss": 71.5, "target": 90.0,
        "plan": {"scale_plan": [{"action": "trim", "trigger_price": 71.5, "pct": 1.0}]},
    }
    proposal = {"action": "hold", "ladder_intent": "preserve", "rationale": "维持"}
    effective = {
        "action": "hold", "ladder_intent": "preserve",
        "effective_stop": 71.5, "effective_target": 90.0,
        "effective_scale_plan": [{"action": "trim", "trigger_price": 71.5, "pct": 1.0}],
    }
    captured = {}
    result_entry = {"analysis_status": "completed", "source": "position_action"}

    monkeypatch.setattr(analyze._cfg_mod, "get", lambda: {
        "deepseek": {"model": "fake", "max_tool_iterations": 1, "history_limit": 1},
    })
    monkeypatch.setattr(analyze, "_make_client", lambda _cfg: object())
    monkeypatch.setattr(analyze, "_load_system_prompt", lambda **_kwargs: "system")
    history_entry = {
        "analyzed_at": "2026-08-30T10:00:00+08:00",
        "verdict": "偏空",
        "analysis_text": "DRAFT_ONLY_HISTORY_PEG_0_43",
    }

    def load_verdicts(**kwargs):
        return [history_entry] if kwargs.get("ts_code") else []

    monkeypatch.setattr(analyze.journal, "load_verdicts", load_verdicts)
    monkeypatch.setattr(analyze.forecast_calibration, "refresh_forecast_rows", lambda _rows: [])
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": [baseline]})
    monkeypatch.setattr(analyze, "_format_history", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(analyze, "_format_intraday_block", lambda _code: ("", {}))
    monkeypatch.setattr(analyze, "_format_market_context", lambda _code: ("", {
        "market_sentiment": {"regime": "亢奋", "private_raw": "drop"},
    }))
    monkeypatch.setattr(analyze, "_format_playstyle_block", lambda _code: ("", {
        "completeness": 0.4,
        "risk_level": "high",
        "features": {"volatility": {"vol_20d_pct": 82.31, "present": True}},
    }))
    def run_loop_spy(**kwargs):
        captured["loop_baseline"] = kwargs["position_baseline"]
        captured["report_context"] = kwargs["report_context"]
        return {
            "analysis_status": "completed", "draft_kind": "position_action",
            "draft_proposal": proposal, "draft_data": effective,
            "analysis_text": "report", "evidence": [], "token_usage": {},
        }

    monkeypatch.setattr(analyze, "_run_langgraph_loop", run_loop_spy)

    def finalize_spy(*args, **kwargs):
        captured["finalize_args"] = args
        captured["finalize_kwargs"] = kwargs
        return result_entry

    monkeypatch.setattr(analyze, "_finalize_position_action", finalize_spy)

    result = analyze.run("000977.SZ", save=True)

    assert result is result_entry
    assert captured["loop_baseline"] == baseline
    assert captured["loop_baseline"] is not baseline
    assert set(captured["report_context"]) == {
        "market", "history", "playstyle", "evidence_selection", "unknowns",
    }
    assert captured["report_context"]["market"]["sentiment"]["regime"] == "亢奋"
    assert captured["report_context"]["history"][0]["verdict"] == "偏空"
    assert "analysis_text" not in repr(captured["report_context"])
    assert "DRAFT_ONLY_HISTORY_PEG_0_43" not in repr(captured["report_context"])
    assert "private_raw" not in repr(captured["report_context"])
    assert captured["finalize_args"][1] == proposal
    assert captured["finalize_args"][2] == effective
    assert captured["finalize_args"][1] != captured["finalize_args"][2]
    assert captured["finalize_kwargs"]["position_baseline"] == baseline
    assert captured["finalize_kwargs"]["position_baseline"] is not baseline


def test_run_persists_same_playstyle_as_report_finalization_metadata(monkeypatch):
    captured = {}
    saved = {}
    playstyle_features = {
        "completeness": 0.8,
        "risk_level": "high",
        "features": {
            "volatility": {"vol_60d_pct": 82.31, "present": True},
            "or_yoy": {
                "latest": 10.0, "quarters_n": 2,
                "sustained_high": False, "present": True,
            },
        },
    }
    candidate = {
        **_bearish_candidate(),
        "playstyle": {
            "ratings": {"波段": 4, "中线": 3},
            "primary": "波段", "secondary": "中线",
            "reasons": ["20日波动率偏高"],
        },
    }

    monkeypatch.setattr(analyze._cfg_mod, "get", lambda: {
        "deepseek": {"model": "fake", "max_tool_iterations": 1, "history_limit": 1},
    })
    monkeypatch.setattr(analyze, "_make_client", lambda _cfg: object())
    monkeypatch.setattr(analyze, "_load_system_prompt", lambda **_kwargs: "system")
    monkeypatch.setattr(analyze.journal, "load_verdicts", lambda **_kwargs: [])
    monkeypatch.setattr(analyze.forecast_calibration, "refresh_forecast_rows", lambda _rows: [])
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})
    monkeypatch.setattr(analyze, "_format_history", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(analyze, "_format_intraday_block", lambda _code: ("", {}))
    monkeypatch.setattr(analyze, "_format_market_context", lambda _code: ("", {}))
    monkeypatch.setattr(
        analyze, "_format_playstyle_block", lambda _code: ("", playstyle_features),
    )
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"002192.SZ": "融捷股份"})
    monkeypatch.setattr(analyze.trace_mod, "write_trace", lambda *_args, **_kwargs: None)

    def run_loop_spy(**kwargs):
        finalized, metadata = kwargs["finalize_candidate"]("verdict", candidate)
        captured["report_metadata"] = metadata
        return {
            "analysis_status": "completed", "draft_kind": "verdict",
            "draft_proposal": {}, "draft_data": finalized,
            "analysis_text": "report", "evidence": [], "token_usage": {},
            "finalization_metadata": {**metadata, "decision_policy": {}},
        }

    monkeypatch.setattr(analyze, "_run_langgraph_loop", run_loop_spy)
    monkeypatch.setattr(analyze.journal, "write_entry", lambda entry: saved.update(entry))

    result = analyze.run("002192.SZ", save=True)

    assert saved["playstyle"] == captured["report_metadata"]["playstyle"]
    assert saved["playstyle_fit"] == captured["report_metadata"]["playstyle_fit"]
    assert saved["risk_level"] == captured["report_metadata"]["risk_level"]
    assert result["playstyle"] == captured["report_metadata"]["playstyle"]


def test_position_report_normalizes_raw_candidate_for_prompt_and_validation(monkeypatch):
    held = {"ts_code": "000977.SZ", "entry_price": 77.0, "position_size_shares": 200,
            "stop_loss": 71.5, "target": 90.0}
    action = {
        "action": "hold", "new_stop": 71.5, "new_target": 90,
        "scale_plan": [
            {"action": "trim", "trigger_price": 71.5, "pct": 1.0, "new_stop": 0},
        ],
    }
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "持仓防守", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_position_action", arguments=action),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=_position_report_with_plan("- trim @ 71.5，比例 1.0")),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": [{
        "title": "浪潮信息公告", "snippet": "未见新增重大风险", "url": "https://www.cninfo.com.cn/scan",
        "date": "2026-08-25", "site": "巨潮资讯", "source_tier": 1,
        "entity_matched": True, "freshness_status": "current",
    }]}))
    monkeypatch.setattr(analyze.data, "get_realtime_price", lambda _codes: {"000977.SZ": 74.0})
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": [held]})
    monkeypatch.setattr(analyze, "_validate_position_action", lambda *args, **kwargs: ([], []))
    seen = []
    original_validator = analyze._validate_final_report
    monkeypatch.setattr(analyze, "_validate_final_report", lambda report, kind, candidate: (
        seen.append(candidate) or original_validator(report, kind, candidate)
    ))

    result = analyze._run_langgraph_loop(
        ts_code="000977.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12,
        emit=lambda _event: None,
    )

    assert result["analysis_status"] == "completed", result
    assert seen[0]["scale_plan"][0]["new_stop"] is None
    prompt = client.calls[-1]["messages"][-1]["content"]
    assert '"new_stop": null' in prompt
    assert "有新止损的档位必须追加" not in prompt
    assert "裁判必须引用权威上下文 decision_policy 中的覆盖率、净硬度和去重结果。" in prompt


def test_ladder_mismatch_reports_expected_and_parsed_values():
    candidate = {
        "action": "hold", "new_stop": 71.5, "new_target": 90,
        "scale_plan": [{"action": "add", "trigger_price": 79.0, "shares": 100}],
    }
    report = _position_report_with_plan("- add @ 80.0，100 股")
    issues = analyze._validate_final_report(report, "position_action", candidate)
    assert any("expected=" in issue and "parsed=" in issue for issue in issues)


def test_apex_graph_loop_uses_tools_assessment_and_independent_review(monkeypatch):
    verdict = {
        "verdict": "中性", "confidence": 5, "entry": 0, "stop_loss": 0, "target": 0,
        "features": {
            "ma5_position": "above", "ma20_position": "below", "volume_ratio": 1.0,
            "rsi_14": 50, "atr_14_pct": 2.0,
        },
        "evidence": ["PE_TTM=20 → 估值中性"], "stock_type": "均衡型",
    }
    client = _FakeClient([
        _response(tool_name="get_fundamentals", arguments={"ts_code": "002050.SZ"}),
        _response(tool_name="submit_research_state", arguments={
            "thesis": "中性", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=verdict),
        _response(content="```json\n" + json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False) + "\n```"),
        _response(content=_complete_verdict_report(verdict="中性", confidence=5).replace(
            "营收与利润数据完整，周期位置仍需谨慎。",
            "PE_TTM=20，营收与利润数据完整，估值处于中性区间。",
        )),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"002050.SZ": "三花智控"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({
        "source": "bocha:web-search", "results": [{
            "title": "三花智控002050风险扫描公告",
            "snippet": "未见新增重大风险事项",
            "url": "https://www.cninfo.com.cn/scan",
            "date": "2026-08-19",
            "site": "巨潮资讯",
            "source_tier": 1,
            "entity_matched": True,
            "freshness_status": "current",
        }], "quality": {"accepted_count": 1},
    }))
    monkeypatch.setattr(analyze, "_dispatch_tool", lambda name, args: json.dumps({
        "valuation": {"pe_ttm": 20}, "quarters": [{"roe": 12}], "summary": {"flags": []},
    }))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})
    traced_tools = []

    class _ToolSpan:
        def __init__(self, item):
            self.item = item

        def set_outputs(self, outputs):
            self.item["outputs"] = outputs

    @contextmanager
    def fake_tool_trace(name, inputs):
        item = {"name": name, "inputs": inputs}
        traced_tools.append(item)
        yield _ToolSpan(item)

    monkeypatch.setattr(observability, "tool_trace", fake_tool_trace)
    events = []

    result = analyze._run_langgraph_loop(
        ts_code="002050.SZ", client=client, model="fake", messages=[{"role": "user", "content": "分析"}],
        max_iter=12, emit=events.append,
    )

    assert result["analysis_status"] == "completed"
    assert result["draft_kind"] == "verdict"
    assert result["draft_data"]["verdict"] == "中性"
    assert "## 核心判断" in result["analysis_text"]
    assert "PE_TTM=20" in result["analysis_text"]
    draft_call = client.calls[2]
    assert [tool["function"]["name"] for tool in draft_call["tools"]] == ["record_verdict"]
    assert draft_call["tool_choice"] == {"type": "function", "function": {"name": "record_verdict"}}
    assert any(event["type"] == "review" and event["outcome"] == "pass" for event in events)
    assert [item["name"] for item in traced_tools] == [
        "authoritative_scan", "get_fundamentals", "submit_research_state", "record_verdict",
    ]
    assert traced_tools[0]["inputs"]["ts_code"] == "002050.SZ"
    assert traced_tools[1]["outputs"]["result"]


def _patch_report_graph_environment(monkeypatch):
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"002192.SZ": "融捷股份"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({
        "source": "bocha:web-search", "results": [{
            "title": "融捷股份002192公告", "snippet": "未见新增重大风险事项",
            "url": "https://www.cninfo.com.cn/scan", "date": "2026-08-25",
            "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
            "freshness_status": "current",
        }],
    }))
    monkeypatch.setattr(analyze, "_dispatch_tool", lambda *_args: json.dumps({
        "valuation": {"pe_ttm": 16.7}, "quarters": [{"roe": 24.8}],
        "summary": {"flags": []},
    }))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})


def _bearish_candidate():
    return {
        "verdict": "观望偏空", "confidence": 4,
        "entry": 0, "stop_loss": 0, "target": 0,
        "features": {
            "ma5_position": "below", "ma20_position": "above", "volume_ratio": 0.77,
            "rsi_14": 45, "atr_14_pct": 5.0,
        },
        "evidence": ["锂价回落 → 周期领先指标转弱"],
        "stock_type": "周期股", "valuation_basis": "non_valuation",
    }


def test_formal_report_replaces_research_process_text(monkeypatch):
    _patch_report_graph_environment(monkeypatch)
    report = _complete_verdict_report()
    client = _FakeClient([
        _response(
            tool_name="get_fundamentals", arguments={"ts_code": "002192.SZ"},
            content="让我查询基本面，等等，重新核算。",
        ),
        _response(tool_name="submit_research_state", arguments={
            "thesis": "观望偏空", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=_bearish_candidate()),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=report),
    ])

    result = analyze._run_langgraph_loop(
        ts_code="002192.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
        system_context="确定性市场上下文：沪深300五日下跌3%",
    )

    assert result["analysis_status"] == "completed"
    assert result["analysis_text"] == report
    assert "让我查询" not in result["analysis_text"]
    assert "tools" not in client.calls[-1]
    report_prompt = client.calls[-1]["messages"][-1]["content"]
    assert "融捷股份002192公告" in report_prompt
    assert "确定性市场上下文：沪深300五日下跌3%" in report_prompt
    assert '"review_outcome": "pass"' in report_prompt


def test_formal_report_prompt_uses_structured_authority_not_research_draft(monkeypatch):
    _patch_report_graph_environment(monkeypatch)
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "观望偏空", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=_bearish_candidate()),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=_complete_verdict_report()),
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
    report_messages = client.calls[-1]["messages"]
    prompt = report_messages[-1]["content"]
    assert '"regime": "亢奋"' in prompt
    assert '"hit": false' in prompt
    assert "DRAFT_ONLY_PEG_0_43" not in repr(report_messages)


def test_formal_report_retries_once_with_validation_issues(monkeypatch):
    _patch_report_graph_environment(monkeypatch)
    incomplete = "## 核心判断\n**判断：观望偏空**\n**置信度：4/10**"
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "观望偏空", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=_bearish_candidate()),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=incomplete),
        _response(content=_complete_verdict_report()),
    ])

    result = analyze._run_langgraph_loop(
        ts_code="002192.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    assert result["analysis_status"] == "completed"
    assert "缺少必需章节：基本面分析" in client.calls[-1]["messages"][-1]["content"]


def test_formal_report_validation_failure_does_not_publish_partial_report(monkeypatch):
    _patch_report_graph_environment(monkeypatch)
    incomplete = "## 核心判断\n**判断：观望偏空**\n**置信度：4/10**"
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "观望偏空", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=_bearish_candidate()),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=incomplete),
        _response(content=incomplete),
    ])

    result = analyze._run_langgraph_loop(
        ts_code="002192.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    assert result["analysis_status"] == "insufficient_evidence"
    assert result["outcome_reason"] == "report_validation_failed"
    assert result.get("analysis_text", "") == ""
    assert any("基本面分析" in issue for issue in result["failures"])


def test_formal_report_provider_failure_preserves_provider_failure_semantics(monkeypatch):
    _patch_report_graph_environment(monkeypatch)
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "观望偏空", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=_bearish_candidate()),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        ConnectionError("report gateway unavailable"),
        TimeoutError("report gateway timeout"),
    ])

    result = analyze._run_langgraph_loop(
        ts_code="002192.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    assert result["analysis_status"] == "insufficient_evidence"
    assert result["outcome_reason"] == "provider_failure"
    assert result["analysis_text"] == ""
    assert any("report gateway timeout" in failure for failure in result["failures"])


def test_formal_report_retry_timeout_is_provider_failure_after_content_rejection(monkeypatch):
    _patch_report_graph_environment(monkeypatch)
    incomplete = "## 核心判断\n**判断：观望偏空**\n**置信度：4/10**"
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "观望偏空", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=_bearish_candidate()),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=incomplete),
        TimeoutError("corrective report timed out"),
    ])

    result = analyze._run_langgraph_loop(
        ts_code="002192.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    assert result["outcome_reason"] == "provider_failure"


def test_formal_report_uses_finalized_candidate_as_single_source_of_truth(monkeypatch):
    _patch_report_graph_environment(monkeypatch)
    raw = _bearish_candidate()
    raw.update({"verdict": "看空", "confidence": 3})
    limited = {**raw, "verdict": "观望偏空", "confidence": 5}
    report = _complete_verdict_report(verdict="观望偏空", confidence=5)
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "看空", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=raw),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=report),
    ])

    result = analyze._run_langgraph_loop(
        ts_code="002192.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12,
        emit=lambda _event: None,
        finalize_candidate=lambda kind, candidate: (
            limited, {"repeat_analysis": {"limited": True, "raw_verdict": candidate["verdict"]}}
        ),
    )

    assert result["analysis_status"] == "completed"
    assert result["draft_data"]["verdict"] == "观望偏空"
    assert result["draft_data"]["confidence"] == 5
    assert result["finalization_metadata"]["repeat_analysis"]["raw_verdict"] == "看空"
    assert result["analysis_text"] == report


def test_formal_report_normalizes_model_coverage_to_finalized_candidate(monkeypatch):
    _patch_report_graph_environment(monkeypatch)
    raw = {**_bearish_candidate(), "verdict": "中性", "confidence": 5}
    finalized = {**raw, "evidence_coverage": 0.6, "counted_evidence_ids": ["sys_test"]}
    bad_report = _complete_verdict_report(verdict="中性", confidence=5).replace(
        "**证据覆盖率：60.0%**",
        "结论：**证据覆盖率：10.0%**；模型又声称**证据覆盖率：90.0%**，等待确认。",
    ).replace(
        "1. 盈利增长 → 提供安全边际。\n2. 现金流改善 → 盈利质量提升。\n3. 负债可控 → 财务风险有限。",
        "- [sys_test] 盈利增长 → 提供安全边际。",
    ).replace(
        "1. 商品价格回落 → 利润可能承压。\n2. 资金净流出 → 短线承接偏弱。\n3. 趋势未反转 → 当前不宜追高。",
        "- [sys_test] 商品价格回落 → 利润可能承压。",
    )
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "观望偏空", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=raw),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=bad_report),
    ])
    result = analyze._run_langgraph_loop(
        ts_code="002192.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12,
        emit=lambda _event: None,
        finalize_candidate=lambda _kind, _candidate: (finalized, {}),
    )

    assert result["analysis_status"] == "completed"
    assert result["analysis_text"].count("**证据覆盖率：60.0%**") == 1
    assert "**证据覆盖率：10.0%**" not in result["analysis_text"]
    assert "**证据覆盖率：90.0%**" not in result["analysis_text"]
    assert "结论：模型又声称，等待确认。" in result["analysis_text"]


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


def test_review_transition_preserves_rework_outcome():
    assert analyze._review_transition(
        "rework", ["需要补充独立来源"], revision_count=0,
    )[0] == "rework"


def test_review_transition_keeps_technical_failure_blocking():
    outcome, messages = analyze._review_transition(
        "abstain", ["独立复核失败: invalid JSON"], revision_count=0,
        technical_failure=True,
    )
    assert outcome == "abstain"
    assert messages == ["独立复核失败: invalid JSON"]


def test_review_transition_does_not_treat_bare_conclusion_direction_as_material():
    issues = analyze._normalize_review_issues(["仅记录结论方向，暂无额外问题"])
    assert issues[0]["severity"] == "minor"
    assert analyze._review_transition(
        "pass", ["仅记录结论方向，暂无额外问题"], revision_count=1,
    )[0] == "pass"


def test_review_transition_treats_legacy_timeframe_wording_as_minor_after_revision():
    issue = "盘中与收盘口径混用，收盘后需补充确认。"

    normalized = analyze._normalize_review_issues([issue])

    assert normalized[0]["severity"] == "minor"
    assert normalized[0]["blocking"] is False
    assert analyze._review_transition("abstain", [issue], revision_count=1)[0] == "pass"


def test_review_transition_escalates_explicit_material_text_mislabeled_minor():
    issue = {
        "message": "候选方向与已确认财务事实存在重大冲突",
        "severity": "minor",
        "blocking": False,
    }

    normalized = analyze._normalize_review_issues([issue])

    assert normalized[0]["severity"] == "material"
    assert normalized[0]["blocking"] is True
    assert analyze._review_transition("pass", [issue], revision_count=0)[0] == "revise"


def test_review_transition_escalates_direction_changing_unsupported_claim():
    issue = {
        "message": "候选方向缺少证据支撑，可能改变结论方向。",
        "severity": "minor",
        "blocking": False,
    }

    normalized = analyze._normalize_review_issues([issue])

    assert normalized[0]["severity"] == "material"
    assert normalized[0]["blocking"] is True


def test_review_transition_escalates_spec_wording_for_unsupported_directional_claim():
    issue = {
        "message": "候选包含无支撑方向性主张，可能误导交易动作。",
        "severity": "minor",
        "blocking": False,
    }

    normalized = analyze._normalize_review_issues([issue])

    assert normalized[0]["severity"] == "material"
    assert normalized[0]["blocking"] is True


def test_bearish_valuation_thesis_cannot_claim_non_valuation_basis():
    candidate = {"verdict": "偏空", "valuation_basis": "non_valuation"}
    assert analyze._valuation_basis_conflict(candidate, "估值仍高，PE_TTM 54.6 压制股价") is True
    assert analyze._valuation_basis_conflict(candidate, "跌破关键均线，技术趋势走弱") is False


def test_material_review_issue_routes_back_to_draft_once(monkeypatch):
    base = {
        "verdict": "偏空", "confidence": 5, "entry": 0, "stop_loss": 0, "target": 0,
        "features": {"ma5_position": "below", "ma20_position": "below", "volume_ratio": 0.8, "rsi_14": 35, "atr_14_pct": 5.8},
        "stock_type": "均衡型", "valuation_basis": "non_valuation",
    }
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={"thesis": "偏空", "gaps": [], "next_actions": [], "ready": True}),
        _response(tool_name="record_verdict", arguments={**base, "evidence": ["单日流出 → 资金持续撤离"]}),
        _response(content=json.dumps({"outcome": "pass", "issues": ["资金面数据存在重大冲突：单日流出不能表述为持续撤离"]}, ensure_ascii=False)),
        _response(tool_name="record_verdict", arguments={**base, "evidence": ["当日主力净流出 → 短线资金偏弱，但此前流入构成反证"]}),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=_complete_verdict_report(verdict="偏空", confidence=5).replace(
            "商品价格和市场波动可能使结论失效。",
            "当日主力净流出，但此前流入构成反证；市场波动可能使结论失效。",
        )),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"002938.SZ": "鹏鼎控股"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": [{
        "title": "鹏鼎控股公告", "snippet": "风险扫描完成", "url": "https://www.cninfo.com.cn/scan",
        "date": "2026-08-24", "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
        "freshness_status": "current",
    }]}))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})
    events = []

    result = analyze._run_langgraph_loop(
        ts_code="002938.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=events.append,
    )

    assert result["analysis_status"] == "completed"
    assert result["review_revision_count"] == 1
    assert "此前流入构成反证" in result["analysis_text"]
    assert [event["outcome"] for event in events if event["type"] == "review"] == ["revise", "pass"]


def test_raw_rework_review_routes_back_to_research_and_reason(monkeypatch):
    base = {
        "verdict": "偏空", "confidence": 5, "entry": 0, "stop_loss": 0, "target": 0,
        "features": {"ma5_position": "below", "ma20_position": "below", "volume_ratio": 0.8, "rsi_14": 35, "atr_14_pct": 5.8},
        "stock_type": "均衡型", "valuation_basis": "non_valuation",
        "evidence": ["主力净流出 → 短线资金偏弱"],
    }
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={"thesis": "偏空", "gaps": [], "next_actions": [], "ready": True}),
        _response(tool_name="record_verdict", arguments=base),
        _response(content=json.dumps({"outcome": "rework", "issues": ["补充独立来源核验资金面"]}, ensure_ascii=False)),
        _response(tool_name="get_fundamentals", arguments={"ts_code": "000977.SZ"}),
        _response(tool_name="submit_research_state", arguments={"thesis": "偏空", "gaps": [], "next_actions": [], "ready": True}),
        _response(tool_name="record_verdict", arguments=base),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=_complete_verdict_report(verdict="偏空", confidence=5)),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": [{
        "title": "浪潮信息公告", "snippet": "风险扫描完成", "url": "https://www.cninfo.com.cn/scan",
        "date": "2026-08-24", "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
        "freshness_status": "current",
    }]}))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})
    events = []

    result = analyze._run_langgraph_loop(
        ts_code="000977.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=events.append,
    )

    assert [event["outcome"] for event in events if event["type"] == "review"][:1] == ["rework"]
    assert sum(event.get("stage") == "researching" for event in events if event["type"] == "status") >= 2


def test_minor_review_issue_passes_after_one_revision_for_000977(monkeypatch):
    base = {
        "verdict": "偏空", "confidence": 5, "entry": 0, "stop_loss": 0, "target": 0,
        "features": {"ma5_position": "below", "ma20_position": "below", "volume_ratio": 0.8, "rsi_14": 35, "atr_14_pct": 5.8},
        "stock_type": "均衡型", "valuation_basis": "non_valuation",
        "evidence": ["主力净流出 → 短线资金偏弱"],
    }
    minor = {"message": "盘中跌破均线但尚未收盘确认", "severity": "minor", "blocking": False}
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={"thesis": "偏空", "gaps": [], "next_actions": [], "ready": True}),
        _response(tool_name="record_verdict", arguments=base),
        _response(content=json.dumps({"outcome": "pass", "issues": [minor]}, ensure_ascii=False)),
        _response(tool_name="record_verdict", arguments=base),
        _response(content=json.dumps({"outcome": "pass", "issues": [minor]}, ensure_ascii=False)),
        _response(content=_complete_verdict_report(verdict="偏空", confidence=5)),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": [{
        "title": "浪潮信息公告", "snippet": "风险扫描完成", "url": "https://www.cninfo.com.cn/scan",
        "date": "2026-08-24", "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
        "freshness_status": "current",
    }]}))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})

    result = analyze._run_langgraph_loop(
        ts_code="000977.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    assert result["analysis_status"] == "completed"
    assert result["review_revision_count"] == 1


def test_minor_reviewer_abstention_after_revision_for_000977(monkeypatch):
    held = {
        "ts_code": "000977.SZ", "entry_price": 77.0, "position_size_shares": 200,
        "stop_loss": 71.5, "target": 90.0,
        "plan": {"scale_plan": [{
            "level": 1, "action": "trim", "trigger_price": 82.0,
            "shares": 100, "new_stop": 71.5, "reason": "历史防守档",
        }]},
    }
    action = {
        "action": "hold", "new_stop": 73.0, "scale_plan": [],
        "rationale": "MA60 已上行，止损随趋势抬升至 73.0。",
    }
    minor = {
        "message": "MA60 上行的确认窗口仍偏短，但不改变持仓防守结论。",
        "severity": "minor", "blocking": False,
    }
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={
            "thesis": "持仓防守", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_position_action", arguments=action),
        _response(content=json.dumps({"outcome": "pass", "issues": [minor]}, ensure_ascii=False)),
        _response(tool_name="record_position_action", arguments=action),
        _response(content=json.dumps({"outcome": "abstain", "issues": [minor]}, ensure_ascii=False)),
        _response(content=_complete_position_report().replace(
            "**当前动作：hold**，保持现有仓位并执行既定风险计划。",
            "**当前动作：hold**\n**当前有效止损：73.0**\n"
            "**当前有效目标：90.0**\nMA60 上行后抬升防守位。",
        ).replace(
            "价格满足计划条件后才执行未来动作，当前不提前交易。",
            "- trim @ 82.0，100 股，新止损 71.5",
        )),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    monkeypatch.setattr(analyze.data, "get_realtime_price", lambda _codes: {"000977.SZ": 74.0})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": [{
        "title": "浪潮信息公告", "snippet": "未见新增重大风险", "url": "https://www.cninfo.com.cn/scan",
        "date": "2026-08-24", "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
        "freshness_status": "current",
    }]}))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": [held]})
    monkeypatch.setattr(analyze.journal, "load_position_actions", lambda _code: [])
    monkeypatch.setattr(analyze, "_finalize_position_action", lambda *_args, **_kwargs: {
        "analysis_status": "completed", "position_action": action,
    })
    events = []

    result = analyze._run_langgraph_loop(
        ts_code="000977.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=events.append,
        system_context=(
            "当前 ladder（历史基线）：L1 trim @ 82.0 -> new_stop 71.5；"
            "当前止损 71.5。"
        ),
        position_baseline=held,
    )

    assert result["analysis_status"] == "completed"
    assert result["review_revision_count"] == 1
    assert [event["outcome"] for event in events if event["type"] == "review"] == ["revise", "pass"]
    review_prompt = next(
        call["messages"][0]["content"]
        for call in client.calls
        if call["messages"][0]["role"] == "system"
        and "独立审稿人" in call["messages"][0]["content"]
    )
    assert "baseline 是冻结的历史持仓基线" in review_prompt
    assert "proposal 是本次提交的拟议修改" in review_prompt
    assert "effective 是唯一需判断的完整结果" in review_prompt


def test_reviewer_first_material_abstention_revises_then_passes(monkeypatch):
    verdict = {
        "verdict": "中性", "confidence": 5, "entry": 0, "stop_loss": 0, "target": 0,
        "features": {"ma5_position": "above", "ma20_position": "below", "volume_ratio": 1.0, "rsi_14": 50, "atr_14_pct": 2.0},
        "evidence": ["结构化数据 → 方向中性"], "stock_type": "均衡型",
    }
    issue = "候选结论与已确认证据存在重大冲突"
    client = _FakeClient([
        _response(tool_name="get_fundamentals", arguments={"ts_code": "002050.SZ"}),
        _response(tool_name="submit_research_state", arguments={"thesis": "中性", "gaps": [], "next_actions": [], "ready": True}),
        _response(tool_name="record_verdict", arguments=verdict),
        _response(content=json.dumps({"outcome": "abstain", "issues": [issue]}, ensure_ascii=False)),
        _response(tool_name="record_verdict", arguments=verdict),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=_complete_verdict_report(verdict="中性", confidence=5)),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"002050.SZ": "三花智控"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": [{
        "title": "三花智控002050公告", "snippet": "风险扫描", "url": "https://www.cninfo.com.cn/scan",
        "date": "2026-08-24", "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
        "freshness_status": "current",
    }]}))
    monkeypatch.setattr(analyze, "_dispatch_tool", lambda *_args: json.dumps({"valuation": {"pe_ttm": 20}}))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})

    events = []
    result = analyze._run_langgraph_loop(
        ts_code="002050.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=events.append,
    )

    assert result["analysis_status"] == "completed"
    assert result["review_revision_count"] == 1
    assert [event["outcome"] for event in events if event["type"] == "review"] == ["revise", "pass"]


def test_second_material_review_contradiction_is_review_failure(monkeypatch):
    base = {
        "verdict": "偏空", "confidence": 5, "entry": 0, "stop_loss": 0, "target": 0,
        "features": {"ma5_position": "below", "ma20_position": "below", "volume_ratio": 0.8, "rsi_14": 35, "atr_14_pct": 5.8},
        "stock_type": "均衡型", "valuation_basis": "non_valuation",
        "evidence": ["当日主力净流出 → 短线资金偏弱"],
    }
    first_issue = "资金面数据存在重大冲突：单日流出不能表述为持续撤离"
    second_issue = "修订后仍存在重大冲突：结论未处理历史流入反证"
    client = _FakeClient([
        _response(tool_name="get_fundamentals", arguments={"ts_code": "002938.SZ"}),
        _response(tool_name="submit_research_state", arguments={"thesis": "偏空", "gaps": [], "next_actions": [], "ready": True}),
        _response(tool_name="record_verdict", arguments=base),
        _response(content=json.dumps({"outcome": "pass", "issues": [first_issue]}, ensure_ascii=False)),
        _response(tool_name="record_verdict", arguments=base),
        _response(content=json.dumps({"outcome": "pass", "issues": [second_issue]}, ensure_ascii=False)),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"002938.SZ": "鹏鼎控股"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": [{
        "title": "鹏鼎控股公告", "snippet": "风险扫描", "url": "https://www.cninfo.com.cn/scan",
        "date": "2026-08-24", "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
        "freshness_status": "current",
    }]}))
    monkeypatch.setattr(analyze, "_dispatch_tool", lambda *_args: json.dumps({"valuation": {"pe_ttm": 20}}))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})

    result = analyze._run_langgraph_loop(
        ts_code="002938.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    assert result["analysis_status"] == "insufficient_evidence"
    assert result["outcome_reason"] == "review_failure"
    assert any(second_issue in failure for failure in result["failures"])


def test_provider_failure_returns_structured_abstention(monkeypatch):
    client = _FakeClient([ConnectionError("model gateway unavailable")])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"603893.SH": "瑞芯微"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({
        "results": [{
            "title": "瑞芯微公告", "snippet": "未见重大风险",
            "url": "https://www.cninfo.com.cn/scan", "date": "2026-08-24",
            "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
            "freshness_status": "current",
        }],
    }))

    events = []
    result = analyze._run_langgraph_loop(
        ts_code="603893.SH", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12,
        emit=events.append,
    )

    assert result["analysis_status"] == "insufficient_evidence"
    assert result["outcome_reason"] == "provider_failure"
    assert result["unknowns"] == []
    assert result["next_actions"] == ["模型服务恢复后重新运行分析"]
    assert "model gateway unavailable" in result["failures"][0]


def test_safety_scan_exception_abstains_without_calling_model(monkeypatch):
    client = _FakeClient([])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    monkeypatch.setattr(
        analyze.data, "web_search",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("安全扫描服务超时")),
    )

    result = analyze._run_langgraph_loop(
        ts_code="000977.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    assert result["analysis_status"] == "insufficient_evidence"
    assert result["outcome_reason"] == "provider_failure"
    assert result["unknowns"] == []
    assert result["next_actions"] == ["权威数据服务恢复后重新运行分析"]
    assert any("安全扫描服务超时" in failure for failure in result["failures"])
    assert client.calls == []


def test_safety_scan_error_payload_abstains_without_calling_model(monkeypatch):
    client = _FakeClient([])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    monkeypatch.setattr(
        analyze.data, "web_search",
        lambda *args, **kwargs: json.dumps({"error": "provider HTTP 503"}),
    )

    result = analyze._run_langgraph_loop(
        ts_code="000977.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    assert result["analysis_status"] == "insufficient_evidence"
    assert result["outcome_reason"] == "provider_failure"
    assert any("HTTP 503" in failure for failure in result["failures"])
    assert client.calls == []


def test_budget_exhaustion_preserves_business_gap_not_internal_stop_code(monkeypatch):
    gap = {"id": "earnings", "description": "最新业绩预告尚未从权威来源核实", "severity": "critical", "status": "open"}
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={"thesis": "待定", "gaps": [gap], "next_actions": ["核实最新业绩预告"], "ready": False}),
        _response(tool_name="submit_research_state", arguments={"thesis": "待定", "gaps": [gap], "next_actions": ["核实最新业绩预告"], "ready": False}),
        _response(tool_name="submit_research_state", arguments={"thesis": "待定", "gaps": [gap], "next_actions": ["核实最新业绩预告"], "ready": False}),
        _response(tool_name="submit_research_state", arguments={"thesis": "待定", "gaps": [gap], "next_actions": ["核实最新业绩预告"], "ready": False}),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"603893.SH": "瑞芯微"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({
        "results": [{
            "title": "瑞芯微公告", "snippet": "风险扫描完成",
            "url": "https://www.cninfo.com.cn/scan", "date": "2026-08-24",
            "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
            "freshness_status": "current",
        }],
    }))

    events = []
    result = analyze._run_langgraph_loop(
        ts_code="603893.SH", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12,
        emit=events.append,
    )

    assert result["analysis_status"] == "insufficient_evidence"
    assert result["outcome_reason"] == "evidence_gap"
    assert result["unknowns"] == ["最新业绩预告尚未从权威来源核实"]
    assert result["next_actions"] == ["核实最新业绩预告"]
    assert result["research_metrics"]["stop_reason"] == "research_round_budget"
    assert result["research_metrics"]["research_rounds"] == 3
    final_status = next(event for event in events if event.get("message") == "补证预算已用尽，正在最终评估")
    assert "current" not in final_status
    assert "total" not in final_status


def test_review_failure_is_persisted_as_unknown_and_failure(monkeypatch):
    verdict = {
        "verdict": "中性", "confidence": 5, "entry": 0, "stop_loss": 0, "target": 0,
        "features": {
            "ma5_position": "above", "ma20_position": "below", "volume_ratio": 1.0,
            "rsi_14": 50, "atr_14_pct": 2.0,
        },
        "evidence": ["结构化数据 → 方向中性"], "stock_type": "均衡型",
    }
    client = _FakeClient([
        _response(tool_name="get_fundamentals", arguments={"ts_code": "002050.SZ"}),
        _response(tool_name="submit_research_state", arguments={
            "thesis": "中性", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=verdict),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"002050.SZ": "三花智控"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({
        "source": "bocha:web-search", "results": [{
            "title": "三花智控002050公告", "snippet": "风险扫描",
            "url": "https://www.cninfo.com.cn/scan", "date": "2026-08-19",
            "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
            "freshness_status": "current",
        }],
    }))
    monkeypatch.setattr(analyze, "_dispatch_tool", lambda *_args: json.dumps({
        "valuation": {"pe_ttm": 20}, "quarters": [{"roe": 12}], "summary": {"flags": []},
    }))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})

    result = analyze._run_langgraph_loop(
        ts_code="002050.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    assert result["analysis_status"] == "insufficient_evidence"
    assert result["outcome_reason"] == "review_failure"
    assert result["unknowns"] == []
    assert any("独立复核失败" in failure for failure in result["failures"])


def test_review_truncation_retries_once_and_recovers(monkeypatch):
    """600487 复读退化：review 输出撞 max_tokens 被截断，重试一次后恢复。"""
    verdict = {
        "verdict": "中性", "confidence": 5, "entry": 0, "stop_loss": 0, "target": 0,
        "features": {
            "ma5_position": "above", "ma20_position": "below", "volume_ratio": 1.0,
            "rsi_14": 50, "atr_14_pct": 2.0,
        },
        "evidence": ["结构化数据 -> 方向中性"], "stock_type": "均衡型",
    }

    def _truncated(content):
        message = SimpleNamespace(content=content, tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="length")])

    client = _FakeClient([
        _response(tool_name="get_fundamentals", arguments={"ts_code": "002050.SZ"}),
        _response(tool_name="submit_research_state", arguments={
            "thesis": "中性", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=verdict),
        # 复核第 1 次：复读到上限被截断，JSON 断在字符串中间
        _truncated('{"outcome": "pass", "issues": ["Unterminated'),
        # 复核第 2 次（重试）：正常返回
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=_complete_verdict_report(verdict="中性", confidence=5)),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"002050.SZ": "三花智控"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({
        "source": "bocha:web-search", "results": [{
            "title": "三花智控002050公告", "snippet": "风险扫描",
            "url": "https://www.cninfo.com.cn/scan", "date": "2026-08-19",
            "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
            "freshness_status": "current",
        }],
    }))
    monkeypatch.setattr(analyze, "_dispatch_tool", lambda *_args: json.dumps({
        "valuation": {"pe_ttm": 20}, "quarters": [{"roe": 12}], "summary": {"flags": []},
    }))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})

    result = analyze._run_langgraph_loop(
        ts_code="002050.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    assert result["analysis_status"] == "completed"


def test_review_parse_failure_exhausting_retries_still_abstains(monkeypatch):
    """重试两次都失败（复读无法打破）时仍按复核失败弃权，不得误判为 pass。"""
    verdict = {
        "verdict": "中性", "confidence": 5, "entry": 0, "stop_loss": 0, "target": 0,
        "features": {
            "ma5_position": "above", "ma20_position": "below", "volume_ratio": 1.0,
            "rsi_14": 50, "atr_14_pct": 2.0,
        },
        "evidence": ["结构化数据 -> 方向中性"], "stock_type": "均衡型",
    }

    def _garbage(content):
        message = SimpleNamespace(content=content, tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    client = _FakeClient([
        _response(tool_name="get_fundamentals", arguments={"ts_code": "002050.SZ"}),
        _response(tool_name="submit_research_state", arguments={
            "thesis": "中性", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_verdict", arguments=verdict),
        _garbage('{"outcome": "pass", "issues": ["Unterminated'),
        _garbage('{"outcome": "pass", "issues": ["Still unterminated'),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"002050.SZ": "三花智控"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({
        "source": "bocha:web-search", "results": [{
            "title": "三花智控002050公告", "snippet": "风险扫描",
            "url": "https://www.cninfo.com.cn/scan", "date": "2026-08-19",
            "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
            "freshness_status": "current",
        }],
    }))
    monkeypatch.setattr(analyze, "_dispatch_tool", lambda *_args: json.dumps({
        "valuation": {"pe_ttm": 20}, "quarters": [{"roe": 12}], "summary": {"flags": []},
    }))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})
    events = []

    result = analyze._run_langgraph_loop(
        ts_code="002050.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=events.append,
    )

    assert result["analysis_status"] == "insufficient_evidence"
    assert any(event["type"] == "review_retry_failed" for event in events)
    assert result["outcome_reason"] == "review_failure"
    assert result["unknowns"] == []


def test_forced_draft_retries_invalid_submission_once(monkeypatch):
    verdict = {
        "verdict": "中性", "confidence": 5, "entry": 0, "stop_loss": 0, "target": 0,
        "features": {"ma5_position": "above", "ma20_position": "below", "volume_ratio": 1.0, "rsi_14": 50, "atr_14_pct": 2.0},
        "evidence": ["结构化数据 → 方向中性"], "stock_type": "均衡型",
    }
    client = _FakeClient([
        _response(tool_name="submit_research_state", arguments={"thesis": "中性", "gaps": [], "next_actions": [], "ready": True}),
        _response(content="没有调用工具"),
        _response(tool_name="record_verdict", arguments=verdict),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=_complete_verdict_report(verdict="中性", confidence=5)),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"603893.SH": "瑞芯微"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": [{
        "title": "瑞芯微公告", "snippet": "风险扫描完成", "url": "https://www.cninfo.com.cn/scan",
        "date": "2026-08-24", "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
        "freshness_status": "current",
    }]}))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})
    events = []

    result = analyze._run_langgraph_loop(
        ts_code="603893.SH", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=events.append,
    )

    assert result["analysis_status"] == "completed"
    assert any(event["type"] == "draft_retry" for event in events)


def test_position_draft_retry_receives_ladder_validation_reason(monkeypatch, isolated_paths):
    held = {
        "ts_code": "000725.SZ", "entry_price": 5.0, "position_size_shares": 1000,
        "stop_loss": 4.5, "target": 7.0,
    }
    invalid = {
        "action": "hold", "rationale": "先减仓后高价接回",
        "scale_plan": [
            {"action": "trim", "trigger_price": 6.0, "shares": 200, "reason": "冲高减仓"},
            {"action": "add", "trigger_price": 6.3, "shares": 200, "reason": "突破加仓"},
        ],
    }
    valid = {
        "action": "hold", "rationale": "保持仓位，不设置互相冲突的加减仓路径",
        "scale_plan": [],
    }
    client = _FakeClient([
        _response(tool_name="get_fundamentals", arguments={"ts_code": "000725.SZ"}),
        _response(tool_name="submit_research_state", arguments={
            "thesis": "持仓谨慎", "gaps": [], "next_actions": [], "ready": True,
        }),
        _response(tool_name="record_position_action", arguments=invalid),
        _response(tool_name="record_position_action", arguments=valid),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=_complete_position_report()),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000725.SZ": "京东方A"})
    monkeypatch.setattr(analyze.data, "get_realtime_price", lambda _codes: {"000725.SZ": 5.0})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": []}))
    monkeypatch.setattr(analyze, "_dispatch_tool", lambda *_args: json.dumps({
        "valuation": {"pe_ttm": 22}, "quarters": [{"roe": 8}], "summary": {"flags": []},
    }))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": [held]})
    monkeypatch.setattr(analyze.journal, "load_position_actions", lambda _code: [])
    monkeypatch.setattr(analyze, "_finalize_position_action", lambda *_args, **_kwargs: {
        "analysis_status": "completed", "position_action": valid,
    })

    result = analyze._run_langgraph_loop(
        ts_code="000725.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    retry_prompt = client.calls[3]["messages"][-1]["content"]
    assert result["analysis_status"] == "completed", result
    assert "trim 之后" in retry_prompt
    assert "add@6.3" in retry_prompt
    assert "churn" in retry_prompt


def test_external_budget_runs_final_assessment_before_abstaining(monkeypatch, isolated_paths):
    held = {
        "ts_code": "000977.SZ", "entry_price": 77.0, "position_size_shares": 200,
        "stop_loss": 71.5, "target": 90.0,
    }
    price_gap = {
        "id": "daily_move", "description": "今日暴跌原因——无明确利空公告",
        "severity": "critical", "status": "open",
    }
    initial_calls = [("submit_research_state", {
        "thesis": "技术走弱", "gaps": [price_gap], "next_actions": ["核实下跌原因"], "ready": False,
    })] + [("get_fundamentals", {"ts_code": "000977.SZ"}) for _ in range(11)]
    action = {"action": "hold", "rationale": "未发现明确利空，依既定止损防守", "scale_plan": []}
    client = _FakeClient([
        _multi_tool_response(initial_calls),
        _response(tool_name="submit_research_state", arguments={
            "thesis": "技术走弱，按止损管理持仓", "gaps": [price_gap],
            "next_actions": ["盘后核实公告"], "ready": True,
        }),
        _response(tool_name="record_position_action", arguments=action),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
        _response(content=_complete_position_report()),
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    monkeypatch.setattr(analyze.data, "get_realtime_price", lambda _codes: {"000977.SZ": 73.2})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": []}))
    monkeypatch.setattr(analyze, "_dispatch_tool", lambda *_args: json.dumps({
        "valuation": {"pe_ttm": 43.9}, "quarters": [{"roe": 8}], "summary": {"flags": []},
    }))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": [held]})
    monkeypatch.setattr(analyze.journal, "load_position_actions", lambda _code: [])
    monkeypatch.setattr(analyze, "_finalize_position_action", lambda *_args, **_kwargs: {
        "analysis_status": "completed", "position_action": action,
    })

    events = []
    result = analyze._run_langgraph_loop(
        ts_code="000977.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=20, emit=events.append,
    )

    assert result["analysis_status"] == "completed", result
    assert len(client.calls[1]["tools"]) == 1
    assert client.calls[1]["tools"][0]["function"]["name"] == "submit_research_state"
    final_status = next(event for event in events if event.get("message") == "补证预算已用尽，正在最终评估")
    assert "current" not in final_status
    assert "total" not in final_status
