"""涨停板池 — tushare limit_list_d (limit='U') 主路径，akshare stock_zt_pool_em 降级。

signal_strength 主要由"连板数"决定：1板 0.4，2板 0.65，3板 0.8，4板+ 0.95。
炸板次数高、封单弱的会扣分。
"""
from typing import Optional

import pandas as pd

from apex import data
from apex.schemas import SignalRecord


def _strength_from_limit_times(limit_times: int, open_times: int = 0) -> float:
    base = {1: 0.4, 2: 0.65, 3: 0.8, 4: 0.92}.get(limit_times, 0.95 if limit_times >= 4 else 0.4)
    if open_times >= 3:
        base -= 0.15
    elif open_times >= 1:
        base -= 0.05
    return max(0.1, min(1.0, base))


def _from_tushare(trade_date: str) -> Optional[pd.DataFrame]:
    pro = data._tushare()
    df = pro.limit_list_d(trade_date=trade_date, limit_type="U")
    if df is None or df.empty:
        return None
    return df


def _from_akshare(trade_date: str) -> Optional[pd.DataFrame]:
    import akshare as ak
    df = ak.stock_zt_pool_em(date=trade_date)
    if df is None or df.empty:
        return None
    rename = {
        "代码": "ts_code",
        "名称": "name",
        "涨跌幅": "pct_chg",
        "最新价": "close",
        "成交额": "amount",
        "封板资金": "fd_amount",
        "首次封板时间": "first_time",
        "最后封板时间": "last_time",
        "炸板次数": "open_times",
        "连板数": "limit_times",
        "所属行业": "industry",
        "流通市值": "float_mv",
        "总市值": "total_mv",
        "换手率": "turnover_ratio",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "ts_code" in df.columns:
        df["ts_code"] = df["ts_code"].astype(str).map(data.normalize_ts_code)
    return df


def fetch(trade_date: str) -> list[SignalRecord]:
    df = None
    try:
        df = _from_tushare(trade_date)
    except Exception:
        df = None

    if df is None or df.empty:
        try:
            df = _from_akshare(trade_date)
        except Exception:
            return []

    if df is None or df.empty or "ts_code" not in df.columns:
        return []

    for col in ("limit_times", "open_times", "fd_amount", "float_mv", "turnover_ratio", "pct_chg"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    records: list[SignalRecord] = []
    for _, row in df.iterrows():
        ts_code = data.normalize_ts_code(str(row.get("ts_code", "")))
        if not ts_code:
            continue

        limit_times = int(row.get("limit_times", 1) or 1)
        open_times = int(row.get("open_times", 0) or 0)
        strength = _strength_from_limit_times(limit_times, open_times)

        records.append({
            "ts_code": ts_code,
            "name": str(row.get("name", "")),
            "signal_type": "limit_up",
            "signal_strength": round(strength, 3),
            "raw": {
                "limit_times": limit_times,
                "open_times": open_times,
                "fd_amount": float(row.get("fd_amount", 0) or 0),
                "first_time": str(row.get("first_time", "") or ""),
                "last_time": str(row.get("last_time", "") or ""),
                "industry": str(row.get("industry", "") or ""),
                "turnover_ratio": float(row.get("turnover_ratio", 0) or 0),
                "float_mv": float(row.get("float_mv", 0) or 0),
            },
        })

    return records
