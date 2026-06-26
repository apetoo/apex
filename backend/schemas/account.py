"""账户相关请求模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class AccountUpdate(BaseModel):
    """账户配置更新，三字段均可选（局部更新）。"""
    total_capital: Optional[float] = None
    risk_per_trade_pct: Optional[float] = None
    max_total_risk_pct: Optional[float] = None
