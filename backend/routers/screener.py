"""今日粗筛接口：报告查询 + SSE 流式执行 + 策略权重。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

from apex import screener as sc
from apex import screener_backtest as sb
from apex.strategies import STRATEGIES

from backend.core.streaming import stream_callback
from backend.schemas.screener import ScreenerRunRequest

router = APIRouter(prefix="/screener", tags=["screener"])


def _wrap_progress(msg: str) -> dict:
    """screener.run 的 on_progress 传字符串，包成结构化事件。"""
    return {"type": "progress", "message": msg}


@router.get("/dates")
def list_dates():
    """可查看的粗筛日期列表。"""
    return sc.list_available_dates()


@router.get("/report")
def get_report(date: Optional[str] = Query(None, description="留空=最新")):
    """读取粗筛报告。date 留空取最新。无报告返回 null。"""
    if not date:
        return sc.load_latest()
    return sc.load_by_date(date)


@router.post("/run")
def run_screener(req: ScreenerRunRequest):
    """执行粗筛，SSE 流式返回进度 + 最终报告。

    事件：
      ``trace`` — ``{type: "progress", message}`` 进度
      ``done``  — 最终粗筛报告 dict
      ``error`` — 失败
    """
    return EventSourceResponse(stream_callback(
        sc.run,
        skip_ai=req.skip_ai,
        skip_selector=req.skip_selector,
        strategy_weights=req.strategy_weights,
        progress_wrapper=_wrap_progress,
    ))


@router.get("/weights")
def default_weights():
    """默认等权策略 + 全部策略名/描述（供前端预设组合配置）。"""
    return {
        "weights": sc.default_weights(),
        "strategies": [
            {"name": name, "description": getattr(mod, "DESCRIPTION", "")}
            for name, mod in STRATEGIES.items()
        ],
    }


@router.get("/factor-ic")
def factor_ic():
    """读取已落盘的因子评测表（IC + pool-alpha + 4 态 verdict）。

    读 ~/.stock-journal/cache/factor_ic.json（快，不跑回测）。
    无则返回 null —— 前端展示"尚未体检"提示。触发回测走 POST /screener/factor-ic/run。
    """
    return sb.load_factor_ic()


@router.post("/factor-ic/run")
def factor_ic_run(no_benchmark: bool = Query(True, description="跳过涨停池基准（首次冷启动池股未缓存会触发大量 tushare 调用 + 1/hour 限速）")):
    """同步触发一次策略体检回测，返回 factor_ic payload。

    注意：同步阻塞调用，涨停池基准开启时可能跑数分钟（池股模拟 + 限速）。
    默认 no_benchmark=True（IC/胜率/verdict 仍全算，仅 pool-alpha 缺）。
    小样本期 verdict 由 n_dates 不足直接判 n_insufficient，基准不影响结论。
    """
    sb._SKIP_LIMIT_UP_BENCHMARK = bool(no_benchmark)
    return sb.run()

