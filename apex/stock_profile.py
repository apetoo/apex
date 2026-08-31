"""Deterministic soft stock classification and blended analysis weights."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


STOCK_TYPE_WEIGHTS: dict[str, dict[str, float]] = {
    "蓝筹白马": {"technical": 0.30, "fundamental": 0.50, "capital": 0.10, "sentiment": 0.10},
    "题材游资": {"technical": 0.35, "fundamental": 0.15, "capital": 0.30, "sentiment": 0.20},
    "周期股": {"technical": 0.25, "fundamental": 0.35, "capital": 0.20, "sentiment": 0.20},
    "成长股": {"technical": 0.30, "fundamental": 0.40, "capital": 0.15, "sentiment": 0.15},
    "均衡型": {"technical": 0.25, "fundamental": 0.25, "capital": 0.25, "sentiment": 0.25},
}

_CYCLICAL_MARKERS = ("钢铁", "煤炭", "有色", "化工", "建材", "航运", "养殖")
_MATURE_MARKERS = ("银行", "保险", "公用", "电力", "食品饮料", "家用电器")
_GROWTH_MARKERS = ("科技", "软件", "半导体", "电子", "医药", "新能源", "汽车零部件", "元器件", "IT")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _number(features: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = features.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


@dataclass(frozen=True)
class StockProfile:
    primary_type: str
    secondary_type: str | None
    membership_scores: dict[str, float]
    classification_confidence: float
    weights: dict[str, float]
    growth_valuation_mode: str
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_stock_profile(features: dict[str, Any]) -> StockProfile:
    """Classify continuously so small market-cap moves cannot flip the framework."""
    circ_mv = _number(features, "circ_mv_yi", "circ_mv", default=0.0)
    # Tushare circ_mv is commonly 万元. Explicit circ_mv_yi wins; otherwise normalize large values.
    if "circ_mv_yi" not in features and circ_mv > 10_000:
        circ_mv /= 10_000
    pe = _number(features, "pe_ttm", "pe", default=0.0)
    turnover = _number(features, "turnover_rate", "turnover", default=0.0)
    revenue_yoy = _number(features, "revenue_yoy", "or_yoy", default=0.0)
    lhb_count = _number(features, "dragon_tiger_count_20d", "dragon_tiger_count", default=0.0)
    industry = str(features.get("industry") or "")

    cyclical_match = any(marker in industry for marker in _CYCLICAL_MARKERS)
    if cyclical_match:
        scores = {name: 0.05 for name in STOCK_TYPE_WEIGHTS}
        scores["周期股"] = 1.0
        return StockProfile(
            primary_type="周期股", secondary_type=None,
            membership_scores=scores, classification_confidence=1.0,
            weights=dict(STOCK_TYPE_WEIGHTS["周期股"]),
            growth_valuation_mode="disabled",
            reasons=[f"industry={industry} 命中周期行业"],
        )

    small_cap = _clamp((160.0 - circ_mv) / 100.0) if circ_mv else 0.35
    medium_cap = _clamp((circ_mv - 80.0) / 100.0) * _clamp((550.0 - circ_mv) / 150.0) if circ_mv else 0.35
    large_cap = _clamp((circ_mv - 300.0) / 400.0) if circ_mv else 0.0
    high_turnover = _clamp((turnover - 3.0) / 7.0)
    lhb_activity = _clamp(lhb_count / 3.0)
    high_pe = 1.0 if pe <= 0 else _clamp((pe - 25.0) / 45.0)
    moderate_pe = _clamp(1.0 - abs(pe - 20.0) / 20.0) if pe > 0 else 0.0
    growth_industry = 1.0 if any(marker in industry for marker in _GROWTH_MARKERS) else 0.35
    mature_industry = 1.0 if any(marker in industry for marker in _MATURE_MARKERS) else 0.15
    revenue_growth = _clamp((revenue_yoy + 5.0) / 30.0)

    theme_score = 0.40 * small_cap + 0.35 * high_turnover + 0.25 * lhb_activity
    growth_score = 0.20 * medium_cap + 0.30 * high_pe + 0.25 * growth_industry + 0.25 * revenue_growth
    blue_score = 0.40 * large_cap + 0.25 * moderate_pe + 0.25 * mature_industry + 0.10 * (1.0 - high_turnover)
    balanced_score = 0.30 + 0.20 * (1.0 - max(theme_score, growth_score, blue_score))

    override = features.get("growth_score_override")
    if override is not None:
        growth_score = _clamp(float(override))

    scores = {
        "蓝筹白马": round(_clamp(blue_score), 4),
        "题材游资": round(_clamp(theme_score), 4),
        "周期股": 0.05,
        "成长股": round(_clamp(growth_score), 4),
        "均衡型": round(_clamp(balanced_score), 4),
    }
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    primary, primary_score = ranked[0]
    secondary, secondary_score = ranked[1]
    if secondary_score < 0.35:
        secondary = None
        blend = {primary: 1.0}
        confidence = 1.0
    else:
        total = primary_score + secondary_score
        blend = {primary: primary_score / total, secondary: secondary_score / total}
        confidence = primary_score / total

    weights = {
        dimension: round(sum(STOCK_TYPE_WEIGHTS[kind][dimension] * share for kind, share in blend.items()), 6)
        for dimension in ("technical", "fundamental", "capital", "sentiment")
    }
    growth_mode = "required" if growth_score >= 0.65 else "mixed" if growth_score >= 0.35 else "disabled"
    reasons = [
        f"circ_mv={circ_mv:.2f}亿", f"pe_ttm={pe:.2f}", f"turnover={turnover:.2f}%",
        f"revenue_yoy={revenue_yoy:.2f}%", f"dragon_tiger_20d={lhb_count:.0f}",
        f"industry={industry or 'unknown'}",
    ]
    return StockProfile(
        primary_type=primary, secondary_type=secondary,
        membership_scores=scores, classification_confidence=round(confidence, 4),
        weights=weights, growth_valuation_mode=growth_mode, reasons=reasons,
    )
