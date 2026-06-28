"""回测接口：实际平仓 + 信号模拟 + 持有期扫描 + 校准切片 + 组合净值 + AI 复盘。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from apex import backtest as bt
from apex import backtest_review as review_mod
from apex import data as data_mod
from apex import watchlist as wl

from backend.core.response import dataframe_records

router = APIRouter(prefix="/backtest", tags=["backtest"])


class ReviewRequest(BaseModel):
    ts_code: Optional[str] = None
    lookforward_days: Optional[int] = None
    model: Optional[str] = None


@router.get("/signals")
def backtest_signals(
    ts_code: Optional[str] = Query(None, description="留空=全部"),
    lookforward_days: int = Query(10, ge=1, le=60),
):
    """信号模拟回测：以看多 verdict 为信号，T+1 开盘入场，逐笔 P&L。"""
    code = data_mod.normalize_ts_code(ts_code) if ts_code else None
    df = bt.run(ts_code=code, lookforward_days=lookforward_days)
    return dataframe_records(df)


@router.get("/signals/sweep")
def backtest_sweep(
    ts_code: Optional[str] = Query(None, description="留空=全部"),
    holding_periods: Optional[str] = Query(
        None, description="逗号分隔，如 1,3,5,10,20；缺省读 config.backtest.holding_periods"),
):
    """持有期扫描：每条信号 × 多档持有期，回答"最优持有几天"。"""
    code = data_mod.normalize_ts_code(ts_code) if ts_code else None
    periods = None
    if holding_periods:
        try:
            periods = [int(p) for p in holding_periods.split(",") if p.strip()]
        except ValueError:
            periods = None
    return bt.run_sweep(ts_code=code, holding_periods=periods)


@router.get("/signals/aggregate")
def backtest_aggregate(
    ts_code: Optional[str] = Query(None, description="留空=全部"),
    lookforward_days: int = Query(10, ge=1, le=60),
):
    """校准切片：按 置信度桶/verdict/source 聚合，回答"AI 自信时准不准"。"""
    code = data_mod.normalize_ts_code(ts_code) if ts_code else None
    return bt.aggregate(ts_code=code, lookforward_days=lookforward_days)


@router.get("/portfolio")
def backtest_portfolio(
    ts_code: Optional[str] = Query(None, description="留空=全部"),
    lookforward_days: int = Query(10, ge=1, le=60),
):
    """组合级净值：全部信号喂进单个 Portfolio（共享资金池）→ 净值曲线 + stats。"""
    code = data_mod.normalize_ts_code(ts_code) if ts_code else None
    return bt.run_portfolio(ts_code=code, lookforward_days=lookforward_days)


@router.get("/realized")
def backtest_realized():
    """实际平仓分析：真实成交，含佣金印花税。"""
    df = bt.run_realized()
    return dataframe_records(df)


@router.post("/review")
def backtest_review(req: ReviewRequest):
    """AI 复盘：对一批回测信号跑 DeepSeek，产出结构化结论 + 落盘策略统计。

    返回 summary / findings / prompt_injection / strategy_weight_hint /
    strategy_stats / aggregate / model / generated_at。BacktestReviewError
    （未配 api_key / 无可成交信号 / AI 调用失败）由全局 handler 映射为 502。
    """
    code = data_mod.normalize_ts_code(req.ts_code) if req.ts_code else None
    return review_mod.review(
        ts_code=code,
        lookforward_days=req.lookforward_days,
        model=req.model,
    )
