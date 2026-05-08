"""概念/题材龙头 — akshare 东财概念板块。

强势题材：当日涨幅 >= 2% 的 Top 5 概念，每概念取涨幅前 2 名作为龙头。
同一只股可能同时在多个题材榜，最终去重时取 strength 最大的那条。
"""
from typing import Optional

import pandas as pd

from apex import data
from apex.schemas import SignalRecord


_TOP_CONCEPTS = 5
_TOP_STOCKS_PER_CONCEPT = 2
_MIN_CONCEPT_PCT = 2.0


def _concept_list() -> Optional[pd.DataFrame]:
    import akshare as ak
    try:
        df = ak.stock_board_concept_name_em()
    except Exception:
        return None
    if df is None or df.empty:
        return None
    rename = {
        "板块名称": "concept_name",
        "板块代码": "concept_code",
        "涨跌幅": "concept_pct",
        "总市值": "concept_mv",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "concept_pct" in df.columns:
        df["concept_pct"] = pd.to_numeric(df["concept_pct"], errors="coerce").fillna(0)
    return df


def _concept_constituents(concept_name: str) -> Optional[pd.DataFrame]:
    import akshare as ak
    try:
        df = ak.stock_board_concept_cons_em(symbol=concept_name)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    rename = {
        "代码": "ts_code",
        "名称": "name",
        "涨跌幅": "pct_change",
        "最新价": "close",
        "成交额": "amount",
        "换手率": "turnover_rate",
        "总市值": "total_mv",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "ts_code" in df.columns:
        df["ts_code"] = df["ts_code"].astype(str).map(data.normalize_ts_code)
    if "pct_change" in df.columns:
        df["pct_change"] = pd.to_numeric(df["pct_change"], errors="coerce").fillna(0)
    return df


def fetch(trade_date: str) -> list[SignalRecord]:
    concepts = _concept_list()
    if concepts is None or concepts.empty or "concept_pct" not in concepts.columns:
        return []

    strong = concepts[concepts["concept_pct"] >= _MIN_CONCEPT_PCT].copy()
    if strong.empty:
        return []

    strong = strong.sort_values("concept_pct", ascending=False).head(_TOP_CONCEPTS)
    max_concept_pct = float(strong["concept_pct"].max()) or 1.0

    by_ts: dict[str, SignalRecord] = {}
    concepts_per_stock: dict[str, list[str]] = {}

    for _, c_row in strong.iterrows():
        concept_name = str(c_row.get("concept_name", "")).strip()
        if not concept_name:
            continue
        concept_pct = float(c_row.get("concept_pct", 0))
        concept_strength = min(1.0, concept_pct / max_concept_pct)

        cons = _concept_constituents(concept_name)
        if cons is None or cons.empty or "ts_code" not in cons.columns:
            continue

        leaders = cons.sort_values("pct_change", ascending=False).head(_TOP_STOCKS_PER_CONCEPT)
        if leaders.empty:
            continue

        max_stock_pct = float(leaders["pct_change"].max()) or 1.0

        for rank, (_, stk) in enumerate(leaders.iterrows(), start=1):
            ts_code = data.normalize_ts_code(str(stk.get("ts_code", "")))
            if not ts_code:
                continue
            stk_pct = float(stk.get("pct_change", 0))
            stk_strength = min(1.0, max(0.0, stk_pct / max_stock_pct)) if max_stock_pct > 0 else 0.0
            strength = 0.6 * concept_strength + 0.4 * stk_strength

            concepts_per_stock.setdefault(ts_code, []).append(concept_name)

            existing = by_ts.get(ts_code)
            if existing is None or strength > existing["signal_strength"]:
                by_ts[ts_code] = {
                    "ts_code": ts_code,
                    "name": str(stk.get("name", "")),
                    "signal_type": "concept",
                    "signal_strength": round(strength, 3),
                    "raw": {
                        "lead_concept": concept_name,
                        "concept_pct": round(concept_pct, 2),
                        "stock_pct": round(stk_pct, 2),
                        "concept_rank": rank,
                    },
                }

    for ts_code, rec in by_ts.items():
        rec["raw"]["all_concepts"] = concepts_per_stock.get(ts_code, [])

    return list(by_ts.values())
