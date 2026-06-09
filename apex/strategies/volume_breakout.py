"""放量突破 —— 量比 > 1.5，股价在近 20 日高位，MA20 上方。

核心逻辑：
  1. 标的从近期涨停池中选取
  2. 量比 > 1.5（显著放量）
  3. 距 20 日最高价 ≤ 5%（近期高位）
  4. 股价在 MA20 上方（中期趋势向上）
  5. 不是放量滞涨（涨幅 > 0）

适用：放量突破关键阻力位的启动点，适合主升浪初期。
买点：次日竞价确认后入场；若放量但高开低走则放弃。
"""
from typing import Optional

from apex import technical as tech

NAME = "volume_breakout"
DESCRIPTION = (
    "放量突破：量比>1.5显著放量，股价在近20日高位（距高点≤5%），"
    "且在MA20上方中期趋势向上。非放量滞涨（涨幅为正）。"
    "适合主升浪启动初期识别。"
    "买点：次日竞价确认放量上涨后入场；若高开低走放量阴线则放弃。"
)

_MIN_VOL_RATIO = 1.5
_MAX_HIGH_DIST = 5.0        # 距 20 日最高 ≤5%
_MIN_LIMIT_COUNT = 0        # 不严格要求有涨停（放量突破本身够强）


def select(by_source: dict, regime: Optional[dict] = None) -> list[dict]:
    limit_up_pool = by_source.get("limit_up_history", []) or []
    bars_cache = by_source.get("_technical", {})
    if not limit_up_pool or not bars_cache:
        return []

    pool_by_ts = {s["ts_code"]: s for s in limit_up_pool}

    # 从所有有日线数据的股票中选（不限于涨停池，但涨停池有日线的）
    out: list[dict] = []
    for ts_code, bars in bars_cache.items():
        if bars is None or len(bars) < 20:
            continue

        # 检查量比
        vr = tech.volume_ratio(bars)
        if vr is None or vr < _MIN_VOL_RATIO:
            continue

        close = float(bars[-1]["close"])
        prev_close = float(bars[-2]["close"]) if len(bars) >= 2 else 0

        # 涨幅为正（排除放量下跌）
        if prev_close > 0 and close <= prev_close:
            continue

        # 距 20 日最高价
        high_dist = tech.high_distance(bars, 20)
        if high_dist is None or high_dist > _MAX_HIGH_DIST:
            continue

        # MA20 上方
        d20 = tech.price_vs_ma20(bars)
        if d20 is None or d20 < 0:
            continue

        # 均线排列（辅助判断）
        alignment = tech.check_alignment(bars)

        # 涨停信息（有最好）
        up_info = pool_by_ts.get(ts_code)
        limit_count = int((up_info.get("raw", {}) or {}).get("limit_count", 0) or 0) if up_info else 0
        name = up_info.get("name", "") if up_info else ts_code.split(".")[0]
        signals = [up_info] if up_info else []

        # 每日涨幅
        chg_pct = (close - prev_close) / prev_close * 100 if prev_close > 0 else 0

        # 量能趋势
        vol_trend = tech.volume_trend(bars, short=5, long=20)

        reasoning_parts = [
            f"放量{vr:.1f}倍"
        ]
        if high_dist is not None:
            reasoning_parts.append(f"，创近20日高位（距顶{high_dist:.1f}%）")
        if d20 is not None:
            reasoning_parts.append(f"，MA20上{d20:.1f}%")
        if alignment == "bullish":
            reasoning_parts.append("，多头排列")
        if limit_count > 0:
            reasoning_parts.append(f"，近{limit_count}次涨停")
        if chg_pct:
            reasoning_parts.append(f"，日涨{chg_pct:.2f}%")

        out.append({
            "ts_code": ts_code,
            "name": name,
            "signals": signals,
            "strategy_reasoning": "".join(reasoning_parts),
            "strategy_features": {
                "vol_ratio": round(vr, 2),
                "high_dist_20d_pct": round(high_dist, 2) if high_dist is not None else None,
                "dist_to_ma20_pct": round(d20, 2) if d20 is not None else None,
                "alignment": alignment or "unknown",
                "limit_count": limit_count,
                "daily_chg_pct": round(chg_pct, 2),
                "vol_trend_5_20": round(vol_trend, 2) if vol_trend is not None else None,
            },
        })
    return out


def score_one(candidate: dict, regime: Optional[dict] = None) -> float:
    f = candidate.get("strategy_features", {}) or {}
    score = 0.4

    # 量比越大越好
    vr = float(f.get("vol_ratio", 0) or 0)
    if vr >= 3.0:
        score += 0.20
    elif vr >= 2.0:
        score += 0.12
    elif vr >= 1.5:
        score += 0.05

    # 距 20 日高点越近越好
    hd = float(f.get("high_dist_20d_pct", 99) or 99)
    if hd <= 1:
        score += 0.15
    elif hd <= 3:
        score += 0.08

    # 均线排列
    if f.get("alignment") == "bullish":
        score += 0.12
    elif f.get("alignment") == "mixed":
        score += 0.05

    # MA20 上方
    d20 = float(f.get("dist_to_ma20_pct", 0) or 0)
    if 1 <= d20 <= 10:
        score += 0.08

    # 有涨停记录加分
    if int(f.get("limit_count", 0) or 0) >= 1:
        score += 0.08

    # 涨幅合理（1-5% 的突破最有效）
    chg = float(f.get("daily_chg_pct", 0) or 0)
    if 2 <= chg <= 5:
        score += 0.05
    elif chg > 8:
        score -= 0.05

    # 量能趋势向上
    vt = float(f.get("vol_trend_5_20", 1) or 1)
    if vt > 1.3:
        score += 0.05

    return max(0.0, min(1.0, score))
