from __future__ import annotations

import pytest

from apex.trade_signal import evaluate_trade_proposal, holding_period_for_setup


def _entry(**overrides):
    entry = {
        "verdict": "偏多",
        "proposed_trade_action": "buy",
        "setup_tag": "趋势突破",
        "features": {"ma5_position": "above", "atr_14_pct": 4.0},
        "price_advice": {
            "entry": 10.0,
            "entry_low": 9.8,
            "entry_high": 10.0,
            "stop_loss": 9.0,
            "target": 11.5,
            "position_size_pct": 10,
            "entry_style": "breakout",
            "valid_for_days": 3,
        },
        "candidate_context": {"source_type": "manual", "red_flag": False},
        "unknowns": [],
    }
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("首板", 1),
        ("题材炒作", 1),
        ("趋势突破", 5),
        ("超跌反弹", 5),
        ("业绩驱动", 10),
        ("其他:事件驱动", None),
        (None, None),
    ],
)
def test_holding_period_for_setup_is_fixed(setup, expected):
    assert holding_period_for_setup(setup) == expected


def test_quality_gate_accepts_complete_strong_trade():
    decision = evaluate_trade_proposal(_entry())

    assert decision["eligible"] is True
    assert decision["action"] == "buy"
    assert decision["holding_period_days"] == 5
    assert decision["reward_risk_after_cost"] >= 1.3
    assert decision["reasons"] == []


@pytest.mark.parametrize("verdict", ["观望偏多", "中性", "偏空"])
def test_quality_gate_rejects_non_actionable_opinions(verdict):
    decision = evaluate_trade_proposal(_entry(verdict=verdict))
    assert decision["action"] == "watch"
    assert "weak_opinion" in decision["reasons"]


def test_quality_gate_preserves_ai_avoid():
    decision = evaluate_trade_proposal(_entry(proposed_trade_action="avoid"))
    assert decision["action"] == "avoid"
    assert "ai_did_not_propose_buy" in decision["reasons"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"features": {"ma5_position": "below", "atr_14_pct": 4}}, "below_ma5"),
        ({"candidate_context": {"source_type": "screener", "red_flag": True}}, "red_flag"),
        ({"unknowns": [{"severity": "critical", "status": "open"}]}, "critical_evidence_gap"),
        ({"setup_tag": "其他:未知"}, "unknown_holding_period"),
    ],
)
def test_quality_gate_rejects_hard_blockers(mutation, reason):
    decision = evaluate_trade_proposal(_entry(**mutation))
    assert decision["eligible"] is False
    assert reason in decision["reasons"]


def test_quality_gate_rejects_invalid_price_plan():
    entry = _entry()
    entry["price_advice"] = {**entry["price_advice"], "stop_loss": 10.2}
    decision = evaluate_trade_proposal(entry)
    assert "invalid_price_plan" in decision["reasons"]


def test_quality_gate_rejects_reward_risk_below_floor_after_costs():
    entry = _entry()
    entry["price_advice"] = {**entry["price_advice"], "target": 11.0}
    decision = evaluate_trade_proposal(entry)
    assert decision["reward_risk_after_cost"] < 1.3
    assert "reward_risk_below_minimum" in decision["reasons"]


def test_quality_gate_requires_positive_position_and_complete_source():
    entry = _entry(candidate_context={})
    entry["price_advice"] = {**entry["price_advice"], "position_size_pct": 0}
    decision = evaluate_trade_proposal(entry)
    assert "zero_position" in decision["reasons"]
    assert "missing_source" in decision["reasons"]
