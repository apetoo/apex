"""机构资金共振 —— 龙虎榜机构席位净买入 + 北向加仓共振。

不依赖涨停。次日可正常成交，actionable 较高。
机构持仓周期通常 5-15 天，对应 swing 持有；游资盘 1-3 天必须严格止损。
"""
from typing import Optional

NAME = "institutional_flow"
DESCRIPTION = (
    "机构资金共振：龙虎榜净买入且上榜原因含'机构专用'席位，与北向加仓榜共振时加分。"
    "不依赖涨停 —— 次日可正常成交，是 actionability 最高的策略之一。"
    "适合震荡市、风险偏好中性偏谨慎、需要资金面验证的环境。"
    "买点：次日开盘正常成交；目标 5-10 天 swing。"
)


def _has_institutional_seat(reasons: list) -> bool:
    if not reasons:
        return False
    for r in reasons:
        rs = str(r)
        if "机构" in rs or "QFII" in rs or "公募" in rs:
            return True
    return False


def select(by_source: dict, regime: Optional[dict] = None) -> list[dict]:
    dragon_tiger = by_source.get("dragon_tiger", []) or []
    northbound = by_source.get("northbound", []) or []
    nb_by_ts = {n["ts_code"]: n for n in northbound}

    out: list[dict] = []
    for s in dragon_tiger:
        raw = s.get("raw", {}) or {}
        net = float(raw.get("net_amount", 0) or 0)
        if net <= 0:
            continue
        reasons = raw.get("reasons", []) or []
        if not _has_institutional_seat(reasons):
            continue

        ts = s["ts_code"]
        nb_resonance = ts in nb_by_ts
        signals = [s]
        if nb_resonance:
            signals.append(nb_by_ts[ts])

        reasoning = f"龙虎榜净买 {net / 1e8:.2f} 亿，含机构席位"
        if nb_resonance:
            inflow = float((nb_by_ts[ts].get("raw", {}) or {}).get("inflow_mv", 0) or 0)
            reasoning += f"，北向同步加仓 {inflow / 1e8:.2f} 亿"
        if reasons:
            tag = next((str(r) for r in reasons if "机构" in str(r)), str(reasons[0]))
            reasoning += f"｜上榜：{tag[:30]}"

        out.append({
            "ts_code": ts,
            "name": s.get("name", "") or (nb_by_ts.get(ts, {}).get("name", "") if nb_resonance else ""),
            "signals": signals,
            "strategy_reasoning": reasoning,
            "strategy_features": {
                "net_amount": net,
                "seats_count": int(raw.get("seats_count", 0) or 0),
                "nb_resonance": nb_resonance,
                "nb_inflow": float((nb_by_ts.get(ts, {}).get("raw", {}) or {}).get("inflow_mv", 0) or 0)
                    if nb_resonance else 0.0,
            },
        })
    return out


def score_one(candidate: dict, regime: Optional[dict] = None) -> float:
    f = candidate.get("strategy_features", {}) or {}
    score = 0.4
    net = float(f.get("net_amount", 0) or 0)
    if net >= 3e8:
        score += 0.30
    elif net >= 1e8:
        score += 0.20
    elif net >= 5e7:
        score += 0.10
    if f.get("nb_resonance"):
        score += 0.20
        if float(f.get("nb_inflow", 0) or 0) >= 5e7:
            score += 0.05
    if int(f.get("seats_count", 0) or 0) >= 3:
        score += 0.10
    return max(0.0, min(1.0, score))
