"""分析相关请求模型。"""
from __future__ import annotations

from pydantic import BaseModel


class PostmortemRunRequest(BaseModel):
    """按 closed_at 定位已平仓记录后重跑 AI 复盘。"""
    closed_at: str
