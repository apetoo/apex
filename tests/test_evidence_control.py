from datetime import datetime, timedelta, timezone

from apex.evidence_control import BudgetConfig, EvidenceController, build_insufficient_entry


def _ev(identifier="ev_1", tier=1, matched=True):
    return {
        "id": identifier,
        "fact": "事实",
        "inference": "推论",
        "evidence_type": "material_event",
        "tool_name": "web_search",
        "source_name": "source",
        "source_url": "https://www.cninfo.com.cn/a",
        "published_at": "2026-08-18",
        "source_tier": tier,
        "entity_matched": matched,
        "freshness_status": "current",
    }


def test_default_budget_matches_product_contract():
    cfg = BudgetConfig()
    assert cfg.max_elapsed_seconds == 300
    assert cfg.max_external_calls == 12
    assert cfg.max_research_rounds == 3
    assert cfg.max_no_progress_rounds == 2
    assert cfg.max_review_reworks == 1


def test_failed_safety_scan_stays_unknown_and_blocks_finalization():
    ctl = EvidenceController()
    ctl.record_safety_scan(success=False, evidence=[])

    decision = ctl.finalization_decision()

    assert ctl.safety_scan_status == "unknown"
    assert decision.allowed is False
    assert "权威黑天鹅扫描失败" in decision.blockers


def test_clean_authoritative_scan_and_no_critical_gaps_can_finalize():
    ctl = EvidenceController()
    ctl.record_safety_scan(success=True, evidence=[])
    ctl.add_evidence([_ev()])
    ctl.submit_assessment(thesis="偏多", gaps=[], ready=True)

    assert ctl.finalization_decision().allowed is True


def test_tier_three_lead_cannot_resolve_critical_gap():
    ctl = EvidenceController()
    ctl.record_safety_scan(success=True, evidence=[])
    ctl.add_evidence([_ev(tier=3)])
    ctl.submit_assessment(
        thesis="看空",
        gaps=[{"id": "event", "description": "疑似重大处罚", "severity": "critical", "status": "open"}],
        ready=True,
    )

    decision = ctl.finalization_decision()
    assert decision.allowed is False
    assert "疑似重大处罚" in decision.blockers


def test_two_no_progress_rounds_stop_research():
    ctl = EvidenceController()
    ctl.record_safety_scan(success=True, evidence=[])
    ctl.submit_assessment(thesis="中性", gaps=[], ready=False)
    assert ctl.should_stop().stop is False
    ctl.submit_assessment(thesis="中性", gaps=[], ready=False)

    assert ctl.should_stop().stop is True
    assert ctl.should_stop().reason == "no_progress"


def test_external_call_and_elapsed_budgets_stop_research():
    ctl = EvidenceController(config=BudgetConfig(max_external_calls=1))
    ctl.record_external_call("web_search", success=True)
    assert ctl.should_stop().reason == "external_call_budget"

    start = datetime(2026, 8, 19, tzinfo=timezone.utc)
    late = start + timedelta(seconds=301)
    ctl = EvidenceController(started_at=start, clock=lambda: late)
    assert ctl.should_stop().reason == "elapsed_budget"


def test_trim_and_exit_require_resolved_authoritative_scan():
    ctl = EvidenceController()
    ctl.add_evidence([_ev()])
    ctl.submit_assessment(thesis="减仓", gaps=[], ready=True)

    assert ctl.finalization_decision(action="trim").allowed is False
    assert ctl.finalization_decision(action="exit").allowed is False


def test_review_allows_only_one_rework():
    ctl = EvidenceController()
    assert ctl.record_review("rework", ["补监管证据"]) == "rework"
    assert ctl.record_review("rework", ["仍然缺失"]) == "abstain"
    assert ctl.review_issues == ["仍然缺失"]


def test_review_rework_requires_new_evidence_and_fresh_assessment():
    ctl = EvidenceController()
    ctl.record_safety_scan(success=True, evidence=[])
    ctl.add_evidence([_ev()])
    ctl.submit_assessment(thesis="偏多", gaps=[], ready=True)

    assert ctl.record_review("rework", ["补充监管证据"]) == "rework"
    assert ctl.finalization_decision().allowed is False
    assert "复核返工尚未取得新增证据并重新评估" in ctl.finalization_decision().blockers

    ctl.add_evidence([_ev("ev_2")])
    assert ctl.finalization_decision().allowed is False
    ctl.submit_assessment(thesis="偏多", gaps=[], ready=True)
    assert ctl.finalization_decision().allowed is True


def test_builds_non_actionable_insufficient_entry():
    entry = build_insufficient_entry(
        ts_code="002050.SZ",
        name="三花智控",
        unknowns=["重大事件无法核实"],
        evidence=[_ev()],
        attempted_tools=["authoritative_scan", "web_search"],
        failures=["web_search timeout"],
        research_summary="已尝试权威来源，仍无法确认。",
        analyzed_at="2026-08-19T10:00:00+08:00",
    )

    assert entry["analysis_status"] == "insufficient_evidence"
    assert entry["verdict"] is None
    assert entry["confidence"] is None
    assert entry["price_advice"] is None
    assert entry["position_action"] is None
    assert entry["unknowns"] == ["重大事件无法核实"]
