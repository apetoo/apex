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
    finalize: Node
    abstain: Node


def _after_reason(state: AnalysisGraphState) -> str:
    return "tools" if state.get("pending_tools") else "assess"


def _after_assess(state: AnalysisGraphState) -> str:
    route = state.get("route") or "abstain"
    return route if route in {"research", "draft", "abstain"} else "abstain"


def _after_review(state: AnalysisGraphState) -> str:
    outcome = state.get("review_outcome") or "abstain"
    return {
        "pass": "finalize",
        "rework": "research",
        "abstain": "abstain",
    }.get(outcome, "abstain")


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
    graph.add_node("finalize", handlers.finalize)
    graph.add_node("abstain", handlers.abstain)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "safety_scan")
    graph.add_edge("safety_scan", "reason")
    graph.add_conditional_edges("reason", _after_reason, {
        "tools": "tools", "assess": "assess",
    })
    graph.add_edge("tools", "assess")
    graph.add_conditional_edges("assess", _after_assess, {
        "research": "reason", "draft": "draft", "abstain": "abstain",
    })
    graph.add_edge("draft", "review")
    graph.add_conditional_edges("review", _after_review, {
        "finalize": "finalize", "research": "reason", "abstain": "abstain",
    })
    graph.add_edge("finalize", END)
    graph.add_edge("abstain", END)
    return graph.compile()
