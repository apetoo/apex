"""个股资金流 — 主力连续净流入（"暗盘"/"偷买"信号）。

取过去 N 个交易日全市场资金流，按 ts_code join 后算：
  consec_days    截至 trade_date，主力净流入为正的连续天数（窗口内上限 N）
  cum_net_inflow 期间累计主力净流入（元）
  cum_pct        期间累计涨跌幅
  max_daily_pct  期间单日最大涨幅（过滤曾涨停）
  float_mv       最新一日流通市值（元，供小市值过滤）
  vol_ratio      最新一日量比（缩量偷买加分，来自 daily_basic）

数据源：tushare moneyflow（主）+ akshare 兜底。金额已在 data.get_moneyflow
入口 *1e4 转成元。窗口内任一日源切换 → source_mixed=True，v1 剔除。

signal_strength: min(consec_days / N, 1.0)。
"""
from typing import Optional

import pandas as pd

from apex import data
from apex.schemas import SignalRecord

_WINDOW = 5            # 回看交易日数
_MIN_CONSEC = 3        # 连续净流入下限
_MIN_CUM_INFLOW_YUAN = 5e7  # 5000 万，过滤噪声

_NAME_CACHE: dict[str, str] = {}


def _ensure_name_cache():
    """tushare moneyflow 不含股票名称，从 stock_basic 拉一次 ts_code→name 映射。"""
    if _NAME_CACHE:
        return
    try:
        df = data._tushare().stock_basic(exchange="", list_status="L",
                                         fields="ts_code,name")
        _NAME_CACHE.update(dict(zip(df["ts_code"].astype(str), df["name"].astype(str))))
    except Exception:
        pass


def _load_window(trade_date: str) -> Optional[pd.DataFrame]:
    """返回过去 _WINDOW 个交易日的合并资金流+日线+市值，按 ts_code 聚合。"""
    import json
    dates = data.last_n_trade_dates(trade_date, _WINDOW)
    if not dates:
        return None

    frames_mf, frames_daily, frames_db = [], [], []
    sources = {}
    for d in dates:
        mf = pd.DataFrame(json.loads(data.get_moneyflow(d) or "[]"))
        if not mf.empty:
            mf = mf[["ts_code", "trade_date", "net_mf_amount", "source"]].copy()
            frames_mf.append(mf)
            # 记录该日主源（同一天内所有行同源）
            sources[d] = mf["source"].iloc[0] if "source" in mf.columns else "tushare"
        daily = pd.DataFrame(json.loads(data.get_market_daily(d) or "[]"))
        if not daily.empty:
            frames_daily.append(daily[["ts_code", "trade_date", "pct_chg", "close"]])
        db = pd.DataFrame(json.loads(data.get_market_daily_basic(d) or "[]"))
        if not db.empty:
            frames_db.append(db[["ts_code", "trade_date", "circ_mv", "volume_ratio"]])

    if not frames_mf:
        return None

    mf = pd.concat(frames_mf, ignore_index=True)
    daily = pd.concat(frames_daily, ignore_index=True) if frames_daily else pd.DataFrame()
    db = pd.concat(frames_db, ignore_index=True) if frames_db else pd.DataFrame()

    mf["net_mf_amount"] = pd.to_numeric(mf["net_mf_amount"], errors="coerce").fillna(0)
    # 标记窗口内是否跨源
    used_sources = set(sources.values())

    out = mf.copy()
    if not daily.empty:
        daily["pct_chg"] = pd.to_numeric(daily["pct_chg"], errors="coerce")
        out = out.merge(daily, on=["ts_code", "trade_date"], how="left")
    if not db.empty:
        db["circ_mv"] = pd.to_numeric(db["circ_mv"], errors="coerce")
        db["volume_ratio"] = pd.to_numeric(db["volume_ratio"], errors="coerce")
        out = out.merge(db, on=["ts_code", "trade_date"], how="left")

    out["source_mixed"] = len(used_sources) > 1
    return out


def _consec_days(group: pd.DataFrame, latest_date: str) -> int:
    """从 latest_date 往回数 net_mf_amount > 0 的连续天数，遇负值即停。"""
    g = group.sort_values("trade_date", ascending=False)
    # 只看 <= latest_date 的交易日（防御：窗口里偶有比 latest 更新的脏数据）
    g = g[g["trade_date"] <= latest_date]
    count = 0
    for _, row in g.iterrows():
        if row["net_mf_amount"] > 0:
            count += 1
        else:
            break
    return count


def fetch(trade_date: str) -> list[SignalRecord]:
    trade_date = (trade_date or "").replace("-", "")
    df = _load_window(trade_date)
    if df is None or df.empty:
        return []

    _ensure_name_cache()

    latest = df["trade_date"].max()
    latest_df = df[df["trade_date"] == latest]

    records: list[SignalRecord] = []
    for ts_code, group in df.groupby("ts_code"):
        if group["source_mixed"].iloc[0]:
            continue  # v1 剔除跨源窗口
        consec = _consec_days(group, latest)
        if consec < _MIN_CONSEC:
            continue

        cum_inflow = float(group["net_mf_amount"].sum())
        if cum_inflow < _MIN_CUM_INFLOW_YUAN:
            continue

        pct = group["pct_chg"].dropna()
        cum_pct = float(((1 + pct / 100).prod() - 1) * 100) if not pct.empty else 0.0
        max_daily_pct = float(pct.max()) if not pct.empty else 0.0

        latest_row = latest_df[latest_df["ts_code"] == ts_code]
        float_mv = float(latest_row["circ_mv"].iloc[0]) if not latest_row.empty and pd.notna(latest_row["circ_mv"].iloc[0]) else 0.0
        vol_ratio = float(latest_row["volume_ratio"].iloc[0]) if not latest_row.empty and pd.notna(latest_row["volume_ratio"].iloc[0]) else 0.0
        name = _NAME_CACHE.get(ts_code, "")

        records.append({
            "ts_code": ts_code,
            "name": name,
            "signal_type": "moneyflow",
            "signal_strength": round(min(consec / _WINDOW, 1.0), 3),
            "raw": {
                "consec_days": consec,
                "cum_net_inflow": round(cum_inflow, 0),
                "cum_pct": round(cum_pct, 2),
                "max_daily_pct": round(max_daily_pct, 2),
                "float_mv": round(float_mv, 0),
                "vol_ratio": round(vol_ratio, 2),
                "source": str(group["source"].iloc[0]) if "source" in group.columns else "tushare",
                "source_mixed": bool(group["source_mixed"].iloc[0]),
                "window_days": _WINDOW,
            },
        })

    return records
