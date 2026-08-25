"""LangGraph orchestration primitives for the Apex analysis agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph


class AnalysisGraphState(TypedDict, total=False):
    ts_code: str
    messages: list[Any]
    pending_tools: list[Any]
    safety_scan_status: str
    evidence: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    research_rounds: int
    new_evidence_count: int
    route: str
    draft: dict[str, Any]
    review_outcome: str | None
    review_issues: list[str]
    analysis_status: str
    result: dict[str, Any]
    assistant_message: Any
    analysis_text: str
    draft_kind: str
    draft_data: dict[str, Any]
    model_iterations: int
    unknowns: list[str]
    attempted_tools: list[str]
    failures: list[str]
    # 未注册的 key 会被 LangGraph 从节点返回值里静默丢弃（token_usage 曾因此
    # 恒为空 dict 落进 journal）。
    token_usage: dict[str, Any]
    outcome_reason: str | None
    next_actions: list[str]
    research_metrics: dict[str, Any]
    draft_route: str
    review_revision_count: int
    final_assessment_requested: bool
    final_assessment_done: bool
    report_route: str
    report_validation_issues: list[str]
    report_generation_attempts: int
    finalization_metadata: dict[str, Any]


Node = Callable[[AnalysisGraphState], dict[str, Any]]


@dataclass
class GraphHandlers:
    prepare: Node
    safety_scan: Node
    reason: Node
    execute_tools: Node
    assess: Node
    draft: Node
    review: Node
    report: Node
    finalize: Node
    abstain: Node


def _after_reason(state: AnalysisGraphState) -> str:
    return "tools" if state.get("pending_tools") else "assess"


def _after_safety_scan(state: AnalysisGraphState) -> str:
    return "assess" if state.get("outcome_reason") else "reason"


def _after_assess(state: AnalysisGraphState) -> str:
    route = state.get("route") or "abstain"
    return route if route in {"research", "draft", "abstain"} else "abstain"


def _after_review(state: AnalysisGraphState) -> str:
    outcome = state.get("review_outcome") or "abstain"
    return {
        "pass": "report",
        "rework": "research",
        "revise": "draft",
        "abstain": "abstain",
    }.get(outcome, "abstain")


def _after_report(state: AnalysisGraphState) -> str:
    return "finalize" if state.get("report_route") == "finalize" else "abstain"


def _after_draft(state: AnalysisGraphState) -> str:
    return "abstain" if state.get("draft_route") == "abstain" else "review"


def build_analysis_graph(handlers: GraphHandlers):
    """Compile the single Apex orchestration path from injected business nodes."""
    graph = StateGraph(AnalysisGraphState)
    graph.add_node("prepare", handlers.prepare)
    graph.add_node("safety_scan", handlers.safety_scan)
    graph.add_node("reason", handlers.reason)
    graph.add_node("tools", handlers.execute_tools)
    graph.add_node("assess", handlers.assess)
    graph.add_node("draft", handlers.draft)
    graph.add_node("review", handlers.review)
    graph.add_node("report", handlers.report)
    graph.add_node("finalize", handlers.finalize)
    graph.add_node("abstain", handlers.abstain)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "safety_scan")
    graph.add_conditional_edges("safety_scan", _after_safety_scan, {
        "reason": "reason", "assess": "assess",
    })
    graph.add_conditional_edges("reason", _after_reason, {
        "tools": "tools", "assess": "assess",
    })
    graph.add_edge("tools", "assess")
    graph.add_conditional_edges("assess", _after_assess, {
        "research": "reason", "draft": "draft", "abstain": "abstain",
    })
    graph.add_conditional_edges("draft", _after_draft, {
        "review": "review", "abstain": "abstain",
    })
    graph.add_conditional_edges("review", _after_review, {
        "report": "report", "research": "reason", "draft": "draft", "abstain": "abstain",
    })
    graph.add_conditional_edges("report", _after_report, {
        "finalize": "finalize", "abstain": "abstain",
    })
    graph.add_edge("finalize", END)
    graph.add_edge("abstain", END)
    return graph.compile()
