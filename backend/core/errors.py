"""apex 业务异常 → HTTP 响应映射。

路由层无需写 try/except 样板：apex 抛出的业务异常在这里统一转成合适的
状态码和 JSON body，前端按 status code 决定交互（如 409 弹"替换/取消"）。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apex import analyze as ana_mod
from apex import backtest_review as br_mod
from apex import chan as chan_mod
from apex import postmortem as pm_mod
from apex import technical as tech_mod
from apex import watchlist as wl_mod

logger = logging.getLogger("apex.backend.errors")


def _json(status: int, payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status, content=payload)


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(wl_mod.DuplicatePositionError)
    async def _dup(_req: Request, exc: wl_mod.DuplicatePositionError) -> JSONResponse:
        # 409 + 现有持仓，前端据此提供"替换旧持仓 / 取消"选择
        return _json(409, {
            "detail": str(exc),
            "ts_code": exc.ts_code,
            "existing": exc.existing,
        })

    @app.exception_handler(wl_mod.PositionNotFoundError)
    async def _not_found(_req: Request, exc: wl_mod.PositionNotFoundError) -> JSONResponse:
        return _json(404, {"detail": str(exc)})

    @app.exception_handler(ana_mod.AnalysisError)
    async def _analysis(_req: Request, exc: ana_mod.AnalysisError) -> JSONResponse:
        return _json(502, {"detail": f"AI 分析失败: {exc}"})

    @app.exception_handler(pm_mod.PostmortemError)
    async def _postmortem(_req: Request, exc: pm_mod.PostmortemError) -> JSONResponse:
        return _json(500, {"detail": f"复盘失败: {exc}"})

    @app.exception_handler(br_mod.BacktestReviewError)
    async def _backtest_review(_req: Request, exc: br_mod.BacktestReviewError) -> JSONResponse:
        # 502 = 上游 AI 调用语义（含未配 key / 无可成交信号 / AI 未产出结论）
        return _json(502, {"detail": f"AI 回测复盘失败: {exc}"})

    @app.exception_handler(chan_mod.ChanUnavailableError)
    async def _chan_unavailable(_req: Request, exc: chan_mod.ChanUnavailableError) -> JSONResponse:
        return _json(501, {"detail": str(exc)})

    @app.exception_handler(tech_mod.DataFetchError)
    async def _data_fetch(_req: Request, exc: tech_mod.DataFetchError) -> JSONResponse:
        # 502 = 上游数据源语义（对齐 AnalysisError 先例）
        return _json(502, {"detail": f"数据获取失败: {exc}"})

    @app.exception_handler(ValueError)
    async def _value_error(_req: Request, exc: ValueError) -> JSONResponse:
        return _json(400, {"detail": str(exc)})

    @app.exception_handler(Exception)
    async def _unhandled(req: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 %s %s", req.method, req.url.path)
        return _json(500, {"detail": f"{type(exc).__name__}: {exc}"})
