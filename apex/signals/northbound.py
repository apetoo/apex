"""北向资金 — akshare 港股通今日增持榜。

取今日净增持金额（市值口径）排名靠前的股票。
signal_strength: |今日增持市值| / 5亿 封顶到 1.0。
默认排除增持金额 < 1000 万的小额变动（噪声）。
"""
from typing import Optional

import pandas as pd

from apex import data
from apex.schemas import SignalRecord


_TOP_N = 30
_MIN_INFLOW_YUAN = 1e7  # 1000 万


def _fetch_hk_hold_today() -> Optional[pd.DataFrame]:
    import akshare as ak
    try:
        df = ak.stock_hsgt_hold_stock_em(market="北向", indicator="今日排行")
    except Exception:
        return None
    if df is None or df.empty:
        return None

    rename_candidates = {
        "代码": "ts_code",
        "名称": "name",
        "今日涨跌幅": "pct_change",
        "今日收盘价": "close",
        "今日持股-市值": "hold_mv",
        "今日增持估计-市值": "inflow_mv",
        "今日增持估计-市值增幅": "inflow_pct",
        "所属板块": "industry",
    }
    df = df.rename(columns={k: v for k, v in rename_candidates.items() if k in df.columns})
    if "ts_code" in df.columns:
        df["ts_code"] = df["ts_code"].astype(str).map(data.normalize_ts_code)
    return df


def fetch(trade_date: str) -> list[SignalRecord]:
    df = _fetch_hk_hold_today()
    if df is None or df.empty or "ts_code" not in df.columns:
        return []

    if "inflow_mv" not in df.columns:
        return []

    df["inflow_mv"] = pd.to_numeric(df["inflow_mv"], errors="coerce").fillna(0)
    if "pct_change" in df.columns:
        df["pct_change"] = pd.to_numeric(df["pct_change"], errors="coerce").fillna(0)

    df = df[df["inflow_mv"] >= _MIN_INFLOW_YUAN].copy()
    if df.empty:
        return []

    df = df.sort_values("inflow_mv", ascending=False).head(_TOP_N)

    records: list[SignalRecord] = []
    for _, row in df.iterrows():
        ts_code = data.normalize_ts_code(str(row.get("ts_code", "")))
        if not ts_code:
            continue
        inflow = float(row.get("inflow_mv", 0))
        strength = min(1.0, inflow / 5e8)

        records.append({
            "ts_code": ts_code,
            "name": str(row.get("name", "")),
            "signal_type": "northbound",
            "signal_strength": round(strength, 3),
            "raw": {
                "inflow_mv": round(inflow, 0),
                "hold_mv": float(row.get("hold_mv", 0) or 0),
                "pct_change": round(float(row.get("pct_change", 0)), 2),
                "industry": str(row.get("industry", "") or ""),
            },
        })

    return records
