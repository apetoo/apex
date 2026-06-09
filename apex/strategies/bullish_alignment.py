"""多头排列 —— MA5 > MA10 > MA20，股价在 MA5 上方运行。

核心逻辑：
  1. 标的从近期涨停池中选取（证明有资金关注）
  2. MA5 > MA10 > MA20 严格多头排列
  3. 股价在 MA5 上方（短期强势）
  4. 股价距 MA5 不超过 8%（不过度超买）
  5. 量比适中 0.8-2.0（有量能支持）

适用：趋势明确的上升市，做趋势跟踪。
买点：沿 MA5 持仓，跌破 MA10 考虑减仓，跌破 MA20 清仓。
"""
from typing import Optional

from apex import technical as tech

NAME = "bullish_alignment"
DESCRIPTION = (
    "均线多头排列：MA5>MA10>MA20 严格多头，股价在MA5上方运行，"
    "且距MA5不超过8%（不过度超买）。量比0.8-2.0确认有量能。"
    "标的需来自近期涨停池（有资金关注）。"
    "适合趋势上升市，做趋势跟踪而非打板。"
    "买点：沿MA5持仓，跌破MA10减仓，跌破MA20清仓。"
)

_MAX_PRICE_TO_MA5 = 8.0     # 距 MA5 最多 8%（不追高）
_MIN_VOL_RATIO = 0.8
_MAX_VOL_RATIO = 2.0
_MIN_LIMIT_COUNT = 1


def select(by_source: dict, regime: Optional[dict] = None) -> list[dict]:
    limit_up_pool = by_source.get("limit_up_history", []) or []
    bars_cache = by_source.get("_technical", {})
    if not limit_up_pool or not bars_cache:
        return []

    out: list[dict] = []
    for s in limit_up_pool:
        ts = s["ts_code"]
        raw = s.get("raw", {}) or {}
        limit_count = int(raw.get("limit_count", 0) or 0)
        if limit_count < _MIN_LIMIT_COUNT:
            continue

        bars = bars_cache.get(ts)
        if bars is None or len(bars) < 20:
            continue

        # 多头排列检查
        alignment = tech.check_alignment(bars)
        if alignment != "bullish":
            continue

        close = float(bars[-1]["close"])
        ma5 = float(bars[-1].get("ma5", 0) or 0)
        if ma5 <= 0:
            continue

        # 股价在 MA5 上方
        dist_to_ma5 = (close - ma5) / ma5 * 100
        if dist_to_ma5 < 0:
            continue
        if dist_to_ma5 > _MAX_PRICE_TO_MA5:
            continue

        # 量能检查
        vr = tech.volume_ratio(bars)
        if vr is not None and (vr < _MIN_VOL_RATIO or vr > _MAX_VOL_RATIO):
            continue

        # 近 20 日区间位置
        high_dist = tech.high_distance(bars, 20)

        # 量能趋势（短期 vs 长期）
        vol_trend = tech.volume_trend(bars, short=5, long=20)

        reasoning_parts = [
            f"近{limit_count}次涨停，多头排列",
            f"，距MA5 {dist_to_ma5:.1f}%",
        ]
        if vr is not None:
            reasoning_parts.append(f"，量比{vr:.2f}")
        if vol_trend is not None:
            reasoning_parts.append(f"，量趋{vol_trend:.2f}")
        if high_dist is not None:
            reasoning_parts.append(f"，距20日高{high_dist:.1f}%")

        out.append({
            "ts_code": ts,
            "name": s.get("name", ""),
            "signals": [s],
            "strategy_reasoning": "".join(reasoning_parts),
            "strategy_features": {
                "dist_to_ma5_pct": round(dist_to_ma5, 2),
                "vol_ratio": round(vr, 2) if vr is not None else None,
                "vol_trend_5_20": round(vol_trend, 2) if vol_trend is not None else None,
                "high_dist_20d_pct": round(high_dist, 2) if high_dist is not None else None,
                "limit_count": limit_count,
                "is_bullish": True,
            },
        })
    return out


def score_one(candidate: dict, regime: Optional[dict] = None) -> float:
    f = candidate.get("strategy_features", {}) or {}
    score = 0.5

    # 距 MA5 距离评分（1-4% 是最佳区间）
    d5 = float(f.get("dist_to_ma5_pct", 0) or 0)
    if 1 <= d5 <= 4:
        score += 0.20
    elif 4 < d5 <= 6:
        score += 0.10
    elif d5 > 6:
        score += 0.0
    elif d5 < 0:
        score -= 0.10

    # 量比适中
    vr = float(f.get("vol_ratio", 1) or 1)
    if 1.0 <= vr <= 1.8:
        score += 0.10
    elif vr > 1.8:
        score += 0.05

    # 量能趋势向上
    vt = float(f.get("vol_trend_5_20", 1) or 1)
    if vt > 1.2:
        score += 0.08
    elif vt < 0.8:
        score -= 0.05

    # 近 20 日高位（说明突破有效）
    hd = float(f.get("high_dist_20d_pct", 99) or 99)
    if hd is not None and hd <= 3:
        score += 0.10

    # 涨停次数
    lc = int(f.get("limit_count", 0) or 0)
    if 1 <= lc <= 3:
        score += 0.05

    return max(0.0, min(1.0, score))
