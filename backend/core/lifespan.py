"""应用生命周期：启动时加载配置、跑一次性迁移。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from apex import config as cfg_module
from apex import watchlist as wl_mod

logger = logging.getLogger("apex.backend.lifespan")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动钩子，对齐 Streamlit 的会话级一次性迁移语义。"""
    cfg_module.load()
    try:
        stats = wl_mod.migrate_and_backfill()
        if any(stats.values()):
            logger.info("watchlist 迁移完成: %s", stats)
    except Exception as exc:  # noqa: BLE001 — 启动期不能因迁移失败而退出
        logger.warning("watchlist 迁移失败（忽略继续启动）: %s", exc)
    yield
