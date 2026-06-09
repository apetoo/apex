"""强势股回踩均线 —— 近期有涨停，现回踩到 MA5/MA10 附近，缩量调整。

核心逻辑：
  1. 过去 20 个交易日内有 ≥1 次涨停（来自 limit_up_history 信号）
  2. 当前价格在 MA5 或 MA10 附近 ±3%（回踩确认）
  3. 量比 ≤1.1（缩量调整，不是放量出货）
  4. 价格在 MA20 上方（中期趋势未破坏）

适用：震荡市 / 趋势市，追高性价比不好时等待回踩入场。
买点：次日回踩到 MA5/MA10 时低吸，止损设前低或 MA20。
"""
from typing import Optional

from apex import technical as tech

NAME = "pullback_to_ma"
DESCRIPTION = (
    "强势股回踩均线：近20日有涨停记录，现价回踩到MA5或MA10附近±3%，"
    "量比≤1.1呈缩量调整，且仍在MA20上方（中期趋势未破）。"
    "适合涨停潮后的分化阶段、结构性行情，不适合单边下跌市。"
    "买点：次日回踩到MA5/MA10时低吸，止损放MA20或近期前低。"
)

_NEAR_MA_THRESHOLD = 3.0   # 离均线 ±3% 算"回踩到"
_MAX_VOL_RATIO = 1.1       # 量比 ≤1.1（缩量）
_MIN_LIMIT_COUNT = 1       # 最少涨停次数
_MAX_DIST_TO_MA20 = 5.0    # 离 MA20 最多 5%（涨幅过大不追）


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

        close = float(bars[-1]["close"])
        ma20 = float(bars[-1].get("ma20", 0) or 0)
        if ma20 <= 0:
            continue

        # 必须在 MA20 上方（中期趋势还在）
        dist_to_ma20 = (close - ma20) / ma20 * 100
        if dist_to_ma20 < 0:
            continue
        if dist_to_ma20 > _MAX_DIST_TO_MA20:
            # 离 MA20 太远 → 涨幅过大，不追
            continue

        # 离 MA5 / MA10 距离
        dist_to_ma5 = tech.ma_distance(bars, 5)
        dist_to_ma10 = tech.ma_distance(bars, 10)
        near_ma5 = dist_to_ma5 is not None and abs(dist_to_ma5) <= _NEAR_MA_THRESHOLD
        near_ma10 = dist_to_ma10 is not None and abs(dist_to_ma10) <= _NEAR_MA_THRESHOLD
        if not (near_ma5 or near_ma10):
            continue

        # 缩量
        vr = tech.volume_ratio(bars)
        if vr is not None and vr > _MAX_VOL_RATIO:
            continue

        # 选出离得最近的那条均线
        dists = []
        if dist_to_ma5 is not None:
            dists.append(("MA5", abs(dist_to_ma5)))
        if dist_to_ma10 is not None:
            dists.append(("MA10", abs(dist_to_ma10)))
        dists.sort(key=lambda x: x[1])
        nearest_ma, nearest_dist = dists[0] if dists else ("MA5", 0)

        # 走势特征
        alignment = tech.check_alignment(bars)
        high_dist = tech.high_distance(bars, 20)

        reasoning_parts = [
            f"近{limit_count}次涨停",
            f"，现价{tech.ma_distance(bars, 5)}%={nearest_ma}",
        ]
        if alignment == "bullish":
            reasoning_parts.append("，均线多头")
        if high_dist is not None and high_dist < 3:
            reasoning_parts.append("，近20日高位附近")
        if vr is not None:
            reasoning_parts.append(f"，量比{vr:.2f}（缩量）")
        reasoning_parts.append(f"，距MA20 {dist_to_ma20:.1f}%")

        out.append({
            "ts_code": ts,
            "name": s.get("name", ""),
            "signals": [s],
            "strategy_reasoning": "".join(reasoning_parts),
            "strategy_features": {
                "near_ma": nearest_ma,
                "near_ma_dist_pct": round(nearest_dist, 2),
                "vol_ratio": round(vr, 2) if vr is not None else None,
                "dist_to_ma20_pct": round(dist_to_ma20, 2),
                "limit_count": limit_count,
                "alignment": alignment or "unknown",
                "high_dist_20d_pct": round(high_dist, 2) if high_dist is not None else None,
            },
        })
    return out


def score_one(candidate: dict, regime: Optional[dict] = None) -> float:
    f = candidate.get("strategy_features", {}) or {}
    score = 0.4

    # 距 MA 越近越好
    near_dist = float(f.get("near_ma_dist_pct", 99) or 99)
    if near_dist <= 1.0:
        score += 0.20
    elif near_dist <= 2.0:
        score += 0.12
    elif near_dist <= 3.0:
        score += 0.05

    # 多头排列加分
    if f.get("alignment") == "bullish":
        score += 0.15
    elif f.get("alignment") == "mixed":
        score += 0.05

    # 均线支撑最好是 MA5（比 MA10 强）
    if f.get("near_ma") == "MA5":
        score += 0.05

    # 缩量程度：量比越低越好
    vr = float(f.get("vol_ratio", 2) or 2)
    if vr <= 0.7:
        score += 0.10
    elif vr <= 0.9:
        score += 0.05

    # 距 MA20 适中（不能太远也不能太近）
    d20 = float(f.get("dist_to_ma20_pct", 0) or 0)
    if 1 <= d20 <= 8:
        score += 0.08
    elif d20 < 0:
        score -= 0.10

    # 涨停次数合理（1-5 次最好）
    lc = int(f.get("limit_count", 0) or 0)
    if 2 <= lc <= 5:
        score += 0.05

    return max(0.0, min(1.0, score))
