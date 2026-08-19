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
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        return next(self._responses)


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
        "source": "bocha:web-search", "results": [], "quality": {"accepted_count": 0},
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
    assert any(event["type"] == "review" and event["outcome"] == "pass" for event in events)
