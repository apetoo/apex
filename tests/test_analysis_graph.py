import json
from types import SimpleNamespace

from apex import analyze
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
        "draft", "review", "finalize",
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


def _multi_tool_response(calls):
    tool_calls = [SimpleNamespace(
        id=f"call-{index}-{name}",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    ) for index, (name, arguments) in enumerate(calls)]
    message = SimpleNamespace(content=None, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")])


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
    events = []

    result = analyze._run_langgraph_loop(
        ts_code="002050.SZ", client=client, model="fake", messages=[{"role": "user", "content": "分析"}],
        max_iter=12, emit=events.append,
    )

    assert result["analysis_status"] == "completed"
    assert result["draft_kind"] == "verdict"
    assert result["draft_data"]["verdict"] == "中性"
    assert "## 最终结论" in result["analysis_text"]
    assert "PE_TTM=20" in result["analysis_text"]
    draft_call = client.calls[2]
    assert [tool["function"]["name"] for tool in draft_call["tools"]] == ["record_verdict"]
    assert draft_call["tool_choice"] == {"type": "function", "function": {"name": "record_verdict"}}
    assert any(event["type"] == "review" and event["outcome"] == "pass" for event in events)


def test_review_pass_with_material_contradiction_requires_draft_revision():
    assert analyze._review_requires_revision("pass", [
        "资金面数据存在矛盾：candidate 声称持续流出，但近两日为净流入",
    ]) is True
    assert analyze._review_requires_revision("pass", ["技术指标存在次要出入，不影响结论"]) is False


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
        _response(content=json.dumps({"outcome": "pass", "issues": ["资金面数据存在矛盾：单日流出不能表述为持续撤离"]}, ensure_ascii=False)),
        _response(tool_name="record_verdict", arguments={**base, "evidence": ["当日主力净流出 → 短线资金偏弱，但此前流入构成反证"]}),
        _response(content=json.dumps({"outcome": "pass", "issues": []}, ensure_ascii=False)),
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


def test_reviewer_business_abstention_is_review_failure(monkeypatch):
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
    ])
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"002050.SZ": "三花智控"})
    monkeypatch.setattr(analyze.data, "web_search", lambda *args, **kwargs: json.dumps({"results": [{
        "title": "三花智控002050公告", "snippet": "风险扫描", "url": "https://www.cninfo.com.cn/scan",
        "date": "2026-08-24", "site": "巨潮资讯", "source_tier": 1, "entity_matched": True,
        "freshness_status": "current",
    }]}))
    monkeypatch.setattr(analyze, "_dispatch_tool", lambda *_args: json.dumps({"valuation": {"pe_ttm": 20}}))
    monkeypatch.setattr("apex.watchlist.load", lambda: {"active_positions": []})

    result = analyze._run_langgraph_loop(
        ts_code="002050.SZ", client=client, model="fake",
        messages=[{"role": "user", "content": "分析"}], max_iter=12, emit=lambda _event: None,
    )

    assert result["analysis_status"] == "insufficient_evidence"
    assert result["outcome_reason"] == "review_failure"
    assert result["unknowns"] == []
    assert any(issue in failure for failure in result["failures"])
    assert result["next_actions"] == ["修正候选结论后重新运行独立复核"]


def test_second_material_review_contradiction_is_review_failure(monkeypatch):
    base = {
        "verdict": "偏空", "confidence": 5, "entry": 0, "stop_loss": 0, "target": 0,
        "features": {"ma5_position": "below", "ma20_position": "below", "volume_ratio": 0.8, "rsi_14": 35, "atr_14_pct": 5.8},
        "stock_type": "均衡型", "valuation_basis": "non_valuation",
        "evidence": ["当日主力净流出 → 短线资金偏弱"],
    }
    first_issue = "资金面数据存在矛盾：单日流出不能表述为持续撤离"
    second_issue = "修订后仍存在矛盾：结论未处理历史流入反证"
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
