from apex import automation


def test_candidate_context_preserves_screener_lineage():
    pick = {
        "strategy": "trend_breakout",
        "signals": [{"signal_type": "limit_up"}, {"signal_type": "industry"}],
        "ai_score": 8,
        "actionable": "high",
        "red_flag": False,
        "regime": {"label": "偏热"},
    }

    context = automation._candidate_context(pick, "20260826")

    assert context == {
        "source_type": "screener",
        "screener_date": "20260826",
        "strategy": "trend_breakout",
        "signals": ["limit_up", "industry"],
        "ai_score": 8,
        "actionable": "high",
        "red_flag": False,
        "regime": {"label": "偏热"},
    }


def test_enforced_mode_only_promotes_gate_approved_buy():
    result = {"verdict": "偏多", "trade_decision": {"eligible": False, "action": "watch"}}
    assert automation._should_promote_analysis(result, ["看多", "偏多"], "shadow") is True
    assert automation._should_promote_analysis(result, ["看多", "偏多"], "enforced") is False

    result["trade_decision"] = {"eligible": True, "action": "buy"}
    assert automation._should_promote_analysis(result, ["看多", "偏多"], "enforced") is True
