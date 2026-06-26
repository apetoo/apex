"""AI 对话相关请求模型。"""
from __future__ import annotations

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str = "user"
    content: str = ""


class ChatStreamRequest(BaseModel):
    """AI 对话流式请求。history 为历史消息（不含当前 message）。"""
    message: str
    history: list[ChatMessage] = []
