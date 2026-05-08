"""首板龙头 —— 当日首板（连板=1）+ 炸板少 + 所属行业当日强势。

适用：题材轮动期 / 风险偏好中性以上；不适用：连板退潮市、跌停潮日。
买点：次日竞价 < +5% 直接挂单；> +5% 等回踩 5 日线或前涨停板价。一字板放弃。
"""
from typing import Optional

NAME = "first_board_leader"
DESCRIPTION = (
    "首板龙头：刚封涨停（连板=1），炸板≤1 次，且所属行业当日整体强势（命中 industry 池）。"
    "适合有题材轮动、风险偏好中性偏积极的市场。退潮 / 跌停潮日表现差。"
    "买点：次日开盘后回踩 5 日线或追入。一字板和封单 < 1 亿不要碰。"
)


def select(by_source: dict, regime: Optional[dict] = None) -> list[dict]:
    limit_up = by_source.get("limit_up", []) or []
    industries = by_source.get("industry", []) or []
    strong_industry_names = {
        (s.get("raw", {}) or {}).get("industry_name", "") for s in industries
    }
    strong_industry_names.discard("")

    out: list[dict] = []
    for s in limit_up:
        raw = s.get("raw", {}) or {}
        try:
            limit_times = int(raw.get("limit_times", 1) or 1)
            open_times = int(raw.get("open_times", 0) or 0)
        except (TypeError, ValueError):
            continue
        if limit_times != 1:
            continue
        if open_times > 1:
            continue

        candidate_industry = str(raw.get("industry", "") or "").strip()
        in_strong = bool(candidate_industry and candidate_industry in strong_industry_names)
        fd_amount = float(raw.get("fd_amount", 0) or 0)
        turnover = float(raw.get("turnover_ratio", 0) or 0)

        reasoning = f"首板（炸板 {open_times} 次）"
        if candidate_industry:
            reasoning += f"，行业 {candidate_industry}"
            if in_strong:
                reasoning += "（强势）"
        if fd_amount > 0:
            reasoning += f"，封单 {fd_amount / 1e8:.2f} 亿"
        if turnover > 0:
            reasoning += f"，换手 {turnover:.1f}%"

        out.append({
            "ts_code": s["ts_code"],
            "name": s.get("name", ""),
            "signals": [s],
            "strategy_reasoning": reasoning,
            "strategy_features": {
                "open_times": open_times,
                "in_strong_industry": in_strong,
                "fd_amount": fd_amount,
                "turnover_ratio": turnover,
                "industry": candidate_industry,
            },
        })
    return out


def score_one(candidate: dict, regime: Optional[dict] = None) -> float:
    f = candidate.get("strategy_features", {}) or {}
    score = 0.5
    if f.get("open_times", 0) == 0:
        score += 0.15
    if f.get("in_strong_industry"):
        score += 0.20
    fd = float(f.get("fd_amount", 0) or 0)
    if fd >= 5e8:
        score += 0.15
    elif fd >= 1e8:
        score += 0.08
    turnover = float(f.get("turnover_ratio", 0) or 0)
    # 换手适中（5-15%）= 有承接但未过度炒作
    if 5 <= turnover <= 15:
        score += 0.05
    elif turnover > 25:
        score -= 0.05
    return max(0.0, min(1.0, score))
