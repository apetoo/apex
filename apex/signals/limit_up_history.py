"""近期涨停历史 — tushare limit_list_d 近 N 个交易日（默认 20）。

提供"近期涨停候选池"，附带涨停次数、最近涨停日期、最高连板等元信息。
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from apex import data
from apex.schemas import SignalRecord

_TZ_CN = timezone(timedelta(hours=8))

_LOOKBACK_DAYS = 40  # 自然日回溯（≈20 个交易日）


def _trade_dates_back(n: int) -> tuple[str, str]:
    """返回 (start_date, end_date) YYYYMMDD 格式，回溯 N 个自然日"""
    end = datetime.now(_TZ_CN).date()
    start = end - timedelta(days=n)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _compute_strength(limit_count: int, max_consecutive: int) -> float:
    """从涨停次数和最高连板估算 signal_strength (0-1)。"""
    base = min(limit_count / 10, 0.5)
    bonus = {1: 0.0, 2: 0.15, 3: 0.30, 4: 0.40}.get(max_consecutive, 0.50 if max_consecutive >= 5 else 0.0)
    return round(min(base + bonus, 1.0), 3)


def _aggregate_by_stock(df: pd.DataFrame) -> dict[str, dict]:
    """按股票聚合多日涨停记录，产出每只股的汇总元信息。"""
    grouped = df.groupby("ts_code")
    out: dict[str, dict] = {}
    for ts_code, grp in grouped:
        grp = grp.sort_values("trade_date", ascending=False)
        rows = grp.to_dict("records")
        limit_dates = [str(r["trade_date"]) for r in rows if r.get("trade_date")]
        limit_count = len(limit_dates)
        if limit_count == 0:
            continue

        # 最高连板数
        max_consecutive = int(grp["limit_times"].max() or 1)

        # 最近一次涨停日期
        last_date = limit_dates[0]

        # 平均封单
        fd_amounts = [float(r.get("fd_amount", 0) or 0) for r in rows]
        avg_fd = sum(fd_amounts) / len(fd_amounts) if fd_amounts else 0

        # 行业（取最近一条）
        industry = str(rows[0].get("industry", "") or "")

        # 股票名称（取最近一条）
        name = str(rows[0].get("name", "") or ts_code)

        strength = _compute_strength(limit_count, max_consecutive)
        max_lt = int(grp["limit_times"].max() or 1)

        # 平均换手
        turnovers = [float(r.get("turnover_ratio", 0) or 0) for r in rows if r.get("turnover_ratio")]
        avg_tr = sum(turnovers) / len(turnovers) if turnovers else 0

        out[ts_code] = {
            "ts_code": ts_code,
            "name": name,
            "signal_type": "limit_up_history",
            "signal_strength": strength,
            "raw": {
                "limit_count": limit_count,
                "max_consecutive": max_consecutive,
                "last_limit_date": last_date,
                "limit_dates": limit_dates[:20],  # 最多记 20 次
                "industry": industry,
                "avg_fd_amount": round(avg_fd, 2),
                "avg_turnover_ratio": round(avg_tr, 2),
                "max_limit_times": int(max_lt),
            },
        }
    return out


def fetch(trade_date: str, lookback_days: int = _LOOKBACK_DAYS) -> list[SignalRecord]:
    """获取近 N 个自然日内有涨停记录的股票清单。"""
    start, end = _trade_dates_back(lookback_days)
    try:
        pro = data._tushare()
        df = pro.limit_list_d(start_date=start, end_date=end, limit_type="U")
    except Exception:
        return []

    if df is None or df.empty or "ts_code" not in df.columns:
        return []

    for col in ("limit_times", "open_times", "fd_amount", "turnover_ratio"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df["ts_code"] = df["ts_code"].astype(str).map(data.normalize_ts_code)
    df["trade_date"] = df["trade_date"].astype(str)

    aggregated = _aggregate_by_stock(df)
    records = list(aggregated.values())

    # 按涨停次数降序排列
    records.sort(key=lambda r: -r["signal_strength"])
    return records
