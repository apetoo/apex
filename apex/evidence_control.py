"""Deterministic evidence, budget, convergence, and abstention controls."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BudgetConfig:
    max_elapsed_seconds: int = 300
    max_external_calls: int = 12
    max_research_rounds: int = 3
    max_no_progress_rounds: int = 2
    max_review_reworks: int = 1


@dataclass(frozen=True)
class StopDecision:
    stop: bool
    reason: str | None = None


@dataclass(frozen=True)
class FinalizationDecision:
    allowed: bool
    blockers: list[str] = field(default_factory=list)


class EvidenceController:
    def __init__(
        self, *, config: BudgetConfig | None = None,
        started_at: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or BudgetConfig()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.started_at = started_at or self._clock()
        self.external_calls = 0
        self.research_rounds = 0
        self.no_progress_rounds = 0
        self.review_reworks = 0
        self.safety_scan_status = "not_run"
        self.evidence: dict[str, dict[str, Any]] = {}
        self.gaps: list[dict[str, Any]] = []
        self.thesis = ""
        self.ready = False
        self.attempted_tools: list[str] = []
        self.failures: list[str] = []
        self.review_issues: list[str] = []
        self._evidence_at_last_assessment = 0
        self._rework_evidence_baseline: int | None = None

    def record_external_call(self, tool_name: str, *, success: bool, error: str = "") -> None:
        self.external_calls += 1
        self.attempted_tools.append(tool_name)
        if not success and error:
            self.failures.append(f"{tool_name}: {error}")

    def record_safety_scan(self, *, success: bool, evidence: list[dict[str, Any]]) -> None:
        self.record_external_call(
            "authoritative_scan", success=success,
            error="权威来源请求失败" if not success else "",
        )
        if not success:
            self.safety_scan_status = "unknown"
            return
        added = self.add_evidence(evidence)
        self.safety_scan_status = "events_found" if added else "clear"

    def add_evidence(self, items: list[dict[str, Any]]) -> int:
        added = 0
        for item in items:
            identifier = str(item.get("id") or "")
            if not identifier or identifier in self.evidence:
                continue
            if not item.get("entity_matched", True):
                continue
            if item.get("freshness_status") in {"future", "invalid"}:
                continue
            self.evidence[identifier] = item
            added += 1
        return added

    def submit_assessment(
        self, *, thesis: str, gaps: list[dict[str, Any]], ready: bool,
    ) -> None:
        self.research_rounds += 1
        self.thesis = thesis
        self.gaps = list(gaps)
        self.ready = bool(ready)
        current = len(self.evidence)
        if current <= self._evidence_at_last_assessment:
            self.no_progress_rounds += 1
        else:
            self.no_progress_rounds = 0
        self._evidence_at_last_assessment = current
        if self._rework_evidence_baseline is not None and current > self._rework_evidence_baseline:
            self._rework_evidence_baseline = None

    def should_stop(self) -> StopDecision:
        elapsed = (self._clock() - self.started_at).total_seconds()
        if elapsed >= self.config.max_elapsed_seconds:
            return StopDecision(True, "elapsed_budget")
        if self.external_calls >= self.config.max_external_calls:
            return StopDecision(True, "external_call_budget")
        if self.research_rounds >= self.config.max_research_rounds:
            return StopDecision(True, "research_round_budget")
        if self.no_progress_rounds >= self.config.max_no_progress_rounds:
            return StopDecision(True, "no_progress")
        return StopDecision(False)

    def finalization_decision(self, *, action: str | None = None) -> FinalizationDecision:
        blockers: list[str] = []
        if self.safety_scan_status in {"not_run", "unknown"}:
            blockers.append("权威黑天鹅扫描失败")
        for gap in self.gaps:
            if gap.get("severity") == "critical" and gap.get("status", "open") != "resolved":
                blockers.append(str(gap.get("description") or gap.get("id") or "关键证据缺口"))
        if not any(int(item.get("source_tier") or 3) <= 2 for item in self.evidence.values()):
            blockers.append("缺少可用于决策的 Tier 1/2 证据")
        material_types = {"material_event", "earnings", "shareholders", "regulatory", "corporate_actions"}
        by_type: dict[str, list[dict[str, Any]]] = {}
        for item in self.evidence.values():
            evidence_type = str(item.get("evidence_type") or "")
            if evidence_type in material_types:
                by_type.setdefault(evidence_type, []).append(item)
        for evidence_type, items in by_type.items():
            if not any(int(item.get("source_tier") or 3) == 2 for item in items):
                continue
            has_tier_one = any(int(item.get("source_tier") or 3) == 1 for item in items)
            independent_sources = set()
            for item in items:
                if int(item.get("source_tier") or 3) != 2:
                    continue
                url = str(item.get("source_url") or "")
                hostname = (urlsplit(url).hostname or "").lower().removeprefix("www.")
                independent_sources.add(hostname or str(item.get("source_name") or ""))
            if not has_tier_one and len(independent_sources - {""}) < 2:
                blockers.append(f"{evidence_type}: Tier 2 重大事实缺少交叉验证")
        if not self.ready:
            blockers.append("Agent 尚未声明证据收敛")
        if self._rework_evidence_baseline is not None:
            blockers.append("复核返工尚未取得新增证据并重新评估")
        if action in {"trim", "exit"} and self.safety_scan_status not in {"clear", "events_found"}:
            blockers.append("减仓/退出前必须完成权威风险核验")
        return FinalizationDecision(not blockers, blockers)

    def record_review(self, outcome: str, issues: list[str] | None = None) -> str:
        self.review_issues = list(issues or [])
        if outcome == "rework":
            if self.review_reworks >= self.config.max_review_reworks:
                return "abstain"
            self.review_reworks += 1
            self._rework_evidence_baseline = len(self.evidence)
            self.ready = False
        return outcome


def build_insufficient_entry(
    *, ts_code: str, name: str | None, unknowns: list[str],
    evidence: list[dict[str, Any]], attempted_tools: list[str],
    failures: list[str], research_summary: str, analyzed_at: str,
    analysis_text: str = "",
) -> dict[str, Any]:
    return {
        "ts_code": ts_code,
        "name": name,
        "date": analyzed_at[:10],
        "analyzed_at": analyzed_at,
        "analysis_status": "insufficient_evidence",
        "source": "standalone",
        "verdict": None,
        "confidence": None,
        "price_advice": None,
        "position_action": None,
        "features": None,
        "evidence": list(evidence),
        "unknowns": list(unknowns),
        "attempted_tools": list(attempted_tools),
        "research_failures": list(failures),
        "research_summary": research_summary,
        "analysis_text": analysis_text,
    }
