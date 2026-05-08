"""行业轮动 —— 强势行业新启动 + 个股涨幅适中（潜在补涨）。

逻辑：行业当日整体强势，但本股涨幅低于行业平均（有补涨空间），且涨幅适中（未追高）。
同时被概念龙头池命中时叠加分。
"""
from typing import Optional

NAME = "industry_rotation"
DESCRIPTION = (
    "行业轮动：当日某行业整体涨幅 ≥1.5%，本股是该行业内涨幅靠前的标的，"
    "或同时被概念龙头池命中。优先选'行业涨多但本股涨少'的滞涨股（补涨潜力），"
    "避免本股涨幅 > 8% 的追高陷阱。适合题材轮动节奏明显、板块效应强的市场。"
    "买点：次日开盘正常成交；持有窗口 3-7 天，行业转弱即出。"
)


def select(by_source: dict, regime: Optional[dict] = None) -> list[dict]:
    industries = by_source.get("industry", []) or []
    concepts = by_source.get("concept", []) or []
    concept_by_ts = {c["ts_code"]: c for c in concepts}

    out: list[dict] = []
    seen: set[str] = set()
    for s in industries:
        ts = s["ts_code"]
        if ts in seen:
            continue
        raw = s.get("raw", {}) or {}
        ind_pct = float(raw.get("industry_pct", 0) or 0)
        stock_pct = float(raw.get("stock_pct", 0) or 0)
        ind_name = str(raw.get("industry_name", "") or "")

        # 个股涨幅 > 8% 视为已追高；< -2% 反向（行业强但股弱可能基本面问题）
        if stock_pct > 8 or stock_pct < -2:
            continue

        concept_match = ts in concept_by_ts
        signals = [s]
        if concept_match:
            signals.append(concept_by_ts[ts])

        reasoning = f"行业 {ind_name} 涨 {ind_pct:.2f}%，个股涨 {stock_pct:.2f}%"
        if concept_match:
            cm_raw = concept_by_ts[ts].get("raw", {}) or {}
            reasoning += f"；概念 {cm_raw.get('lead_concept', '')} 涨 {cm_raw.get('concept_pct', 0)}%"
        if stock_pct < ind_pct:
            reasoning += "（行业内滞涨）"

        out.append({
            "ts_code": ts,
            "name": s.get("name", ""),
            "signals": signals,
            "strategy_reasoning": reasoning,
            "strategy_features": {
                "industry_pct": ind_pct,
                "stock_pct": stock_pct,
                "industry_name": ind_name,
                "concept_match": concept_match,
                "is_laggard": stock_pct < ind_pct,
            },
        })
        seen.add(ts)
    return out


def score_one(candidate: dict, regime: Optional[dict] = None) -> float:
    f = candidate.get("strategy_features", {}) or {}
    score = 0.3
    ind_pct = float(f.get("industry_pct", 0) or 0)
    if ind_pct >= 4:
        score += 0.25
    elif ind_pct >= 2.5:
        score += 0.18
    elif ind_pct >= 1.5:
        score += 0.10
    if f.get("concept_match"):
        score += 0.18
    if f.get("is_laggard"):
        score += 0.15
    stock_pct = float(f.get("stock_pct", 0) or 0)
    # 偏好 1-5% 区间（不追高，但有动能）
    if 1 <= stock_pct <= 5:
        score += 0.12
    elif stock_pct > 6:
        score -= 0.05
    return max(0.0, min(1.0, score))
