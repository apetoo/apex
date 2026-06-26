"""行情相关请求模型（多数用 query param，此处预留 body 场景）。"""
from __future__ import annotations

from pydantic import BaseModel


class PricesRequest(BaseModel):
    """批量行情请求（也可用 GET query）。"""
    codes: list[str]
