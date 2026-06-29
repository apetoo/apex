"""主力偷买 —— 连续多日主力净流入为正、期间股价涨幅温和，即资金在涨停前悄悄进场。

不依赖涨停/龙虎榜（那些是事后明牌）。次日可正常成交，actionability 较高。
适合 swing / neutral regime；risk_on 高情绪连板市降权（连板淹没 + 出货前对倒风险）。

买点：次日开盘正常成交；目标 5-10 天 swing。止损：跌破偷买区间低点或 -5%。
"""
from typing import Optional

NAME = "stealth_accumulation"
DESCRIPTION = (
    "主力偷买：连续 3+ 日主力净流入为正、期间累计涨幅温和（≤8%）、未现单日异动，"
    "即资金在涨停/上龙虎榜前悄悄进场。不依赖涨停，次日可正常成交，actionability 高。"
    "适合 swing / neutral regime；risk_on 高情绪连板市应降权——连板淹没信号 + 出货前对倒风险。"
    "买点：次日开盘正常成交；目标 5-10 天 swing。"
)

# v1 先验阈值，待回测/dry-run 标定
_MIN_CUM_PCT = -10.0    # 期间累计涨幅下限（跌超 10% 视为非健康偷买——出货对倒/散户恐慌出逃）
_MAX_CUM_PCT = 8.0      # 期间累计涨幅上限（温和，没大涨）
_MAX_DAILY_PCT = 5.0    # 单日最大涨幅上限（没涨停/异动）
_VOL_RATIO_MAX = 2.0    # 量比上限（未明显放量，"偷"买）


def select(by_source: dict, regime: Optional[dict] = None) -> list[dict]:
    moneyflow = by_source.get("moneyflow", []) or []
    out: list[dict] = []
    for s in moneyflow:
        raw = s.get("raw", {}) or {}
        consec = int(raw.get("consec_days", 0) or 0)
        if consec < 3:
            continue
        cum_pct = float(raw.get("cum_pct", 0) or 0)
        if cum_pct > _MAX_CUM_PCT or cum_pct < _MIN_CUM_PCT:
            continue
        max_daily = float(raw.get("max_daily_pct", 0) or 0)
        if max_daily > _MAX_DAILY_PCT:
            continue
        vol_ratio = float(raw.get("vol_ratio", 0) or 0)
        if vol_ratio > _VOL_RATIO_MAX:
            continue

        inflow = float(raw.get("cum_net_inflow", 0) or 0)
        reasoning = (
            f"主力连续 {consec} 日净流入累计 {inflow / 1e8:.2f} 亿，"
            f"期间涨 {cum_pct:.1f}%（单日最高 {max_daily:.1f}%），量比 {vol_ratio:.2f}"
        )

        out.append({
            "ts_code": s["ts_code"],
            "name": s.get("name", ""),
            "signals": [s],
            "strategy_reasoning": reasoning[:80],
            "strategy_features": {
                "consec_days": consec,
                "cum_net_inflow": inflow,
                "cum_pct": cum_pct,
                "max_daily_pct": max_daily,
                "vol_ratio": vol_ratio,
                "float_mv": float(raw.get("float_mv", 0) or 0),
            },
        })
    return out


def score_one(candidate: dict, regime: Optional[dict] = None) -> float:
    f = candidate.get("strategy_features", {}) or {}
    score = 0.40  # 与同类策略对齐（institutional_flow=0.4, leader_with_volume=0.45）

    consec = int(f.get("consec_days", 0) or 0)
    if consec >= 5:
        score += 0.25
    elif consec >= 3:
        score += 0.15

    inflow = float(f.get("cum_net_inflow", 0) or 0)
    if inflow >= 3e8:
        score += 0.20
    elif inflow >= 1e8:
        score += 0.12

    cum_pct = float(f.get("cum_pct", 0) or 0)
    if cum_pct <= 3.0:
        score += 0.15  # 越安静越像偷买
    elif cum_pct <= 8.0:
        score += 0.08

    vol_ratio = float(f.get("vol_ratio", 0) or 0)
    if 0 < vol_ratio < 1.0:
        score += 0.10  # 缩量偷买加分

    return max(0.0, min(1.0, score))
