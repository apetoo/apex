"""回测接口：实际平仓分析 + 信号模拟回测。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from apex import backtest as bt
from apex import data as data_mod
from apex import watchlist as wl

from backend.core.response import dataframe_records

router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.get("/signals")
def backtest_signals(
    ts_code: Optional[str] = Query(None, description="留空=全部"),
    lookforward_days: int = Query(10, ge=1, le=60),
):
    """信号模拟回测：以看多 verdict 为信号，T+1 收盘入场。"""
    code = data_mod.normalize_ts_code(ts_code) if ts_code else None
    df = bt.run(ts_code=code, lookforward_days=lookforward_days)
    return dataframe_records(df)


@router.get("/realized")
def backtest_realized():
    """实际平仓分析：真实成交，含佣金印花税。"""
    df = bt.run_realized()
    return dataframe_records(df)
