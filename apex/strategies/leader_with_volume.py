"""龙头放量 —— 涨停 + 龙虎榜双重确认。

区别于纯涨停：龙虎榜上榜=有大资金参与，不是纯封板锁盘的"挂单涨停"。
4+ 板高度龙头主动剔除（actionable 太低）。
"""
from typing import Optional

NAME = "leader_with_volume"
DESCRIPTION = (
    "龙头放量：当日涨停 + 龙虎榜上榜（双重确认有真实资金参与）。"
    "比纯涨停多一层资金面验证，比纯龙虎榜多一层情绪面验证。"
    "4 连板及以上主动剔除（次日 actionable 太低）。"
    "适合风险偏好高、市场有龙头带动的环境。"
    "买点：次日竞价强势可追；炸板回封类不要碰。仓位严控 1-2%。"
)


def select(by_source: dict, regime: Optional[dict] = None) -> list[dict]:
    limit_up = {s["ts_code"]: s for s in (by_source.get("limit_up", []) or [])}
    dragon_tiger = {s["ts_code"]: s for s in (by_source.get("dragon_tiger", []) or [])}
    common = set(limit_up) & set(dragon_tiger)

    out: list[dict] = []
    for ts in common:
        lu = limit_up[ts]
        dt = dragon_tiger[ts]
        lu_raw = lu.get("raw", {}) or {}
        dt_raw = dt.get("raw", {}) or {}
        try:
            limit_times = int(lu_raw.get("limit_times", 1) or 1)
            open_times = int(lu_raw.get("open_times", 0) or 0)
        except (TypeError, ValueError):
            continue
        if limit_times >= 4:
            continue

        net = float(dt_raw.get("net_amount", 0) or 0)
        is_net_buy = net > 0

        reasoning = (
            f"{limit_times} 连板（炸板 {open_times}），"
            f"龙虎榜净{'买' if is_net_buy else '卖'} {abs(net) / 1e8:.2f} 亿"
        )

        out.append({
            "ts_code": ts,
            "name": lu.get("name", "") or dt.get("name", ""),
            "signals": [lu, dt],
            "strategy_reasoning": reasoning,
            "strategy_features": {
                "limit_times": limit_times,
                "open_times": open_times,
                "net_amount": net,
                "is_net_buy": is_net_buy,
                "fd_amount": float(lu_raw.get("fd_amount", 0) or 0),
            },
        })
    return out


def score_one(candidate: dict, regime: Optional[dict] = None) -> float:
    f = candidate.get("strategy_features", {}) or {}
    score = 0.45
    if f.get("is_net_buy"):
        score += 0.20
    abs_net = abs(float(f.get("net_amount", 0) or 0))
    if abs_net >= 3e8:
        score += 0.15
    elif abs_net >= 1e8:
        score += 0.08
    limit_times = int(f.get("limit_times", 1) or 1)
    if limit_times == 1:
        score += 0.10
    elif limit_times == 2:
        score += 0.05
    elif limit_times == 3:
        score -= 0.05
    open_times = int(f.get("open_times", 0) or 0)
    if open_times >= 3:
        score -= 0.20
    elif open_times >= 1:
        score -= 0.05
    return max(0.0, min(1.0, score))
