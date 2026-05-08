"""龙虎榜 — tushare top_list 主路径，akshare stock_lhb_detail_em 降级。

每只 ts_code 只产出一条聚合记录：合并多上榜理由，net_amount 求和。
signal_strength: |net_amount| / 5亿 封顶到 1.0。
"""
from typing import Optional

import pandas as pd

from apex import data
from apex.schemas import SignalRecord


def _from_tushare(trade_date: str) -> Optional[pd.DataFrame]:
    pro = data._tushare()
    df = pro.top_list(trade_date=trade_date)
    if df is None or df.empty:
        return None
    return df


def _from_akshare(trade_date: str) -> Optional[pd.DataFrame]:
    import akshare as ak
    iso = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    df = ak.stock_lhb_detail_em(start_date=iso, end_date=iso)
    if df is None or df.empty:
        return None
    rename = {
        "代码": "ts_code",
        "名称": "name",
        "收盘价": "close",
        "涨跌幅": "pct_change",
        "换手率": "turnover_rate",
        "龙虎榜净买额": "net_amount",
        "龙虎榜买入额": "l_buy",
        "龙虎榜卖出额": "l_sell",
        "龙虎榜成交额": "l_amount",
        "上榜原因": "reason",
        "解读": "reason",
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

    if df is None or df.empty:
        return []

    if "ts_code" not in df.columns:
        return []

    for col in ("net_amount", "l_buy", "l_sell", "l_amount", "pct_change"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    records: list[SignalRecord] = []
    for ts_code, group in df.groupby("ts_code"):
        ts_code = data.normalize_ts_code(str(ts_code))
        net_amount = float(group["net_amount"].sum()) if "net_amount" in group else 0.0
        l_buy = float(group["l_buy"].sum()) if "l_buy" in group else 0.0
        l_sell = float(group["l_sell"].sum()) if "l_sell" in group else 0.0
        reasons = []
        if "reason" in group.columns:
            reasons = sorted({str(r) for r in group["reason"].dropna().tolist() if r})
        name = str(group["name"].iloc[0]) if "name" in group.columns and len(group) else ""
        pct = float(group["pct_change"].iloc[0]) if "pct_change" in group.columns and len(group) else 0.0

        strength = min(1.0, abs(net_amount) / 5e8) if net_amount else 0.0

        records.append({
            "ts_code": ts_code,
            "name": name,
            "signal_type": "dragon_tiger",
            "signal_strength": round(strength, 3),
            "raw": {
                "net_amount": round(net_amount, 0),
                "l_buy": round(l_buy, 0),
                "l_sell": round(l_sell, 0),
                "pct_change": round(pct, 2),
                "reasons": reasons,
                "seats_count": int(len(group)),
                "is_net_buy": net_amount > 0,
            },
        })

    return records
