"""今日粗筛接口：报告查询 + SSE 流式执行 + 策略权重。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

from apex import screener as sc
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
