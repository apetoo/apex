"""Versioned, deterministic gate between an AI opinion and an executable trade."""
from __future__ import annotations

from typing import Optional

from apex import config


GATE_VERSION = "v1"
MIN_REWARD_RISK = 1.3
DEFAULT_VALID_FOR_DAYS = 3

_HOLDING_PERIODS = {
    "打板": 1,
    "首板": 1,
    "龙回头": 1,
    "题材炒作": 1,
    "板块轮动": 5,
    "超跌反弹": 5,
    "趋势突破": 5,
    "低位反转": 5,
    "业绩驱动": 10,
}


def holding_period_for_setup(setup_tag: Optional[str]) -> Optional[int]:
    """Map a pre-declared setup to an immutable official holding period."""
    if not setup_tag:
        return None
    return _HOLDING_PERIODS.get(str(setup_tag))


def _costs(override: Optional[dict]) -> dict[str, float]:
    if override is None:
        override = ((config.get().get("backtest") or {}).get("costs") or {})
    return {
        "commission_rate": float(override.get("commission_rate", 0.00025)),
        "stamp_duty_rate": float(override.get("stamp_duty_rate", 0.001)),
        "slippage": float(override.get("slippage", 0.001)),
    }


def _reward_risk(price_advice: dict, costs: dict[str, float]) -> Optional[float]:
    try:
        entry_high = float(price_advice["entry_high"])
        stop = float(price_advice["stop_loss"])
        target = float(price_advice["target"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 < stop < entry_high < target:
        return None

    commission = costs["commission_rate"]
    stamp = costs["stamp_duty_rate"]
    slip = costs["slippage"]
    buy_cost = entry_high * (1 + commission + slip)
    target_proceeds = target * (1 - commission - stamp - slip)
    stop_proceeds = stop * (1 - commission - stamp - slip)
    reward = target_proceeds / buy_cost - 1
    risk = 1 - stop_proceeds / buy_cost
    if reward <= 0 or risk <= 0:
        return None
    return round(reward / risk, 4)


def evaluate_trade_proposal(entry: dict, costs: Optional[dict] = None) -> dict:
    """Return the authoritative V1 action without mutating the AI proposal."""
    verdict = str(entry.get("opinion_verdict") or entry.get("verdict") or "")
    proposed = str(entry.get("proposed_trade_action") or "watch")
    price_advice = dict(entry.get("price_advice") or {})
    features = entry.get("features") or {}
    candidate = entry.get("candidate_context") or {}
    reasons: list[str] = []
    risk_flags: list[str] = []

    if proposed != "buy":
        reasons.append("ai_did_not_propose_buy")
    if verdict not in {"看多", "偏多"}:
        reasons.append("weak_opinion")
    if candidate.get("source_type") not in {"manual", "screener"}:
        reasons.append("missing_source")
    if bool(candidate.get("red_flag")):
        reasons.append("red_flag")
    if any(
        isinstance(gap, dict)
        and gap.get("severity") == "critical"
        and gap.get("status", "open") == "open"
        for gap in (entry.get("unknowns") or [])
    ):
        reasons.append("critical_evidence_gap")

    if features.get("ma5_position") != "above":
        reasons.append("below_ma5" if features.get("ma5_position") == "below" else "missing_critical_data")
    if features.get("atr_14_pct") is None:
        reasons.append("missing_critical_data")

    holding_period = holding_period_for_setup(entry.get("setup_tag"))
    if holding_period is None:
        reasons.append("unknown_holding_period")

    entry_style = price_advice.get("entry_style")
    valid_for_days = price_advice.get("valid_for_days", DEFAULT_VALID_FOR_DAYS)
    try:
        entry_low = float(price_advice["entry_low"])
        entry_high = float(price_advice["entry_high"])
        entry_anchor = float(price_advice["entry"])
        stop = float(price_advice["stop_loss"])
        target = float(price_advice["target"])
        valid_for_days = int(valid_for_days)
        price_valid = (
            entry_style in {"pullback", "breakout"}
            and 0 < stop < entry_low <= entry_anchor <= entry_high < target
            and 1 <= valid_for_days <= 10
        )
    except (KeyError, TypeError, ValueError):
        price_valid = False
        entry_low = entry_high = entry_anchor = stop = target = None
    if not price_valid:
        reasons.append("invalid_price_plan")

    try:
        position = int(price_advice.get("position_size_pct") or 0)
    except (TypeError, ValueError):
        position = 0
    if position <= 0:
        reasons.append("zero_position")

    rr = _reward_risk(price_advice, _costs(costs)) if price_valid else None
    if rr is not None and rr < MIN_REWARD_RISK:
        reasons.append("reward_risk_below_minimum")
    elif rr is None and price_valid:
        reasons.append("invalid_price_plan")

    try:
        if float(features.get("atr_14_pct")) > 6:
            risk_flags.append("high_atr")
    except (TypeError, ValueError):
        pass
    playstyle_features = ((entry.get("playstyle_features") or {}).get("features") or {})
    try:
        vol20 = float(((playstyle_features.get("volatility") or {}).get("vol_20d_pct")))
        if vol20 > 70:
            risk_flags.append("high_volatility")
    except (TypeError, ValueError):
        pass

    reasons = list(dict.fromkeys(reasons))
    eligible = not reasons
    action = "buy" if eligible else ("avoid" if proposed == "avoid" else "watch")
    return {
        "gate_version": GATE_VERSION,
        "proposed_action": proposed,
        "action": action,
        "eligible": eligible,
        "reasons": reasons,
        "risk_flags": risk_flags,
        "holding_period_days": holding_period,
        "reward_risk_after_cost": rr,
        "entry_plan": {
            "style": entry_style,
            "low": entry_low,
            "high": entry_high,
            "anchor": entry_anchor,
            "valid_for_days": valid_for_days,
            "stop_loss": stop,
            "target": target,
        },
    }
