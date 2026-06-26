"""粗筛相关请求模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ScreenerRunRequest(BaseModel):
    """粗筛流式请求。strategy_weights 传入则跳过 AI selector。"""
    skip_ai: bool = False
    skip_selector: bool = False
    strategy_weights: Optional[dict[str, float]] = None
