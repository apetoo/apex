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
        "as_of": "20260826",
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


def test_versioned_candidate_trigger_uses_band_without_chasing_gaps():
    pullback = {
        "entry_style": "pullback", "trigger_low": 9.8, "trigger_high": 10.2,
    }
    breakout = {
        "entry_style": "breakout", "trigger_low": 9.8, "trigger_high": 10.2,
    }

    assert automation._triggered_today(pullback, {"open": 10.5, "high": 10.6, "low": 10.0, "close": 10.1}) is True
    assert automation._triggered_today(pullback, {"open": 9.5, "high": 10.0, "low": 9.2, "close": 9.4}) is False
    assert automation._triggered_today(breakout, {"open": 9.5, "high": 10.0, "low": 9.4, "close": 9.9}) is True
    assert automation._triggered_today(breakout, {"open": 10.5, "high": 10.8, "low": 10.0, "close": 10.7}) is False
