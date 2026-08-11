"""Apex FastAPI 应用入口。

启动：
    uvicorn backend.main:app --reload --port 8000
文档：
    http://localhost:8000/docs
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from backend.core.errors import register_exception_handlers
from backend.core.lifespan import lifespan
from backend.routers import (
    account,
    analyze,
    backtest,
    calibration,
    chan,
    chat,
    market,
    postmortem,
    push,
    screener,
    sector_sentiment,
    system,
    watchlist,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(
    title="Apex API",
    description="A 股交易系统后端 —— 行情 / 持仓 / AI 分析 / 回测 / 粗筛 / 对话",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",   # vite dev
        "http://localhost:4173",   # vite preview
        "http://localhost:8501",   # streamlit
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

# ── 路由注册 ──────────────────────────────────────────────────────────────────
for rtr in (
    market.router,
    watchlist.router,
    watchlist.trigger_router,
    account.router,
    analyze.router,
    analyze.journal_router,
    analyze.trace_router,
    backtest.router,
    chan.router,
    screener.router,
    sector_sentiment.router,
    calibration.router,
    postmortem.router,
    push.router,
    system.router,
    chat.router,
):
    app.include_router(rtr, prefix="/api")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/api/health", tags=["health"])
def health():
    return {"status": "ok"}
