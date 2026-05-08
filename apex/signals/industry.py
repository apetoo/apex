"""申万/东财行业涨幅 — 取当日强势行业 Top 5，每行业取强势股 Top 3。

强势定义：行业涨跌幅 >= 1.5% 且非负。每只股票记录的 strength
= 0.5 × 行业强度 + 0.5 × 股票在行业内涨幅排名分。

注意：akshare 东财接口取的是"当前"快照，不按 trade_date 历史回看。
本 fetcher 假设在收盘后当天运行；trade_date 仅用于元数据标注。
"""
from typing import Optional

import pandas as pd

from apex import data
from apex.schemas import SignalRecord


_TOP_INDUSTRIES = 5
_TOP_STOCKS_PER_INDUSTRY = 3
_MIN_INDUSTRY_PCT = 1.5


def _industry_list() -> Optional[pd.DataFrame]:
    import akshare as ak
    df = ak.stock_board_industry_name_em()
    if df is None or df.empty:
        return None
    rename = {
        "板块名称": "industry_name",
        "板块代码": "industry_code",
        "涨跌幅": "industry_pct",
        "总市值": "industry_mv",
        "上涨家数": "up_count",
        "下跌家数": "down_count",
        "领涨股票": "leader_name",
        "领涨股票-涨跌幅": "leader_pct",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "industry_pct" in df.columns:
        df["industry_pct"] = pd.to_numeric(df["industry_pct"], errors="coerce").fillna(0)
    return df


def _industry_constituents(industry_name: str) -> Optional[pd.DataFrame]:
    import akshare as ak
    try:
        df = ak.stock_board_industry_cons_em(symbol=industry_name)
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
    industries = _industry_list()
    if industries is None or industries.empty:
        return []
    if "industry_pct" not in industries.columns:
        return []

    strong = industries[industries["industry_pct"] >= _MIN_INDUSTRY_PCT].copy()
    if strong.empty:
        return []

    strong = strong.sort_values("industry_pct", ascending=False).head(_TOP_INDUSTRIES)

    records: list[SignalRecord] = []
    max_industry_pct = float(strong["industry_pct"].max()) or 1.0

    for ind_rank, (_, ind_row) in enumerate(strong.iterrows(), start=1):
        industry_name = str(ind_row.get("industry_name", "")).strip()
        if not industry_name:
            continue
        industry_pct = float(ind_row.get("industry_pct", 0))
        industry_strength = min(1.0, industry_pct / max_industry_pct)

        cons = _industry_constituents(industry_name)
        if cons is None or cons.empty or "ts_code" not in cons.columns:
            continue

        cons = cons.sort_values("pct_change", ascending=False).head(_TOP_STOCKS_PER_INDUSTRY)
        if cons.empty:
            continue

        max_stock_pct = float(cons["pct_change"].max()) or 1.0

        for _, stk in cons.iterrows():
            ts_code = data.normalize_ts_code(str(stk.get("ts_code", "")))
            if not ts_code:
                continue
            stk_pct = float(stk.get("pct_change", 0))
            stk_strength_in_ind = min(1.0, max(0.0, stk_pct / max_stock_pct)) if max_stock_pct > 0 else 0.0
            strength = 0.5 * industry_strength + 0.5 * stk_strength_in_ind

            records.append({
                "ts_code": ts_code,
                "name": str(stk.get("name", "")),
                "signal_type": "industry",
                "signal_strength": round(strength, 3),
                "raw": {
                    "industry_name": industry_name,
                    "industry_pct": round(industry_pct, 2),
                    "stock_pct": round(stk_pct, 2),
                    "industry_rank_in_top": ind_rank,
                },
            })

    return records
