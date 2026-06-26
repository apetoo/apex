"""通用模型与别名。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class MessageResponse(BaseModel):
    """简单消息响应。"""
    message: str
    data: Optional[dict] = None


class AckRequest(BaseModel):
    """触发确认请求：单个或批量 signature。"""
    signature: Optional[str] = None
    signatures: Optional[list[str]] = None


class TriggerCheckRequest(BaseModel):
    """触发检查请求，可传入预取行情避免重复拉取。"""
    notify_enabled: bool = False
    prices: Optional[dict[str, float]] = None
