"""持仓 / 候选相关请求模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AddPositionRequest(BaseModel):
    """新增持仓。ts_code 会经 normalize_ts_code 规范化。"""
    ts_code: str
    name: Optional[str] = None
    entry_price: float
    stop_loss: float
    target: float
    trigger_price: Optional[float] = None
    trigger_direction: str = "below"
    expires_days: int = 10
    position_size_shares: Optional[int] = None
    risk_amount: Optional[float] = None
    calibrated_confidence: Optional[float] = None
    strategy: Optional[str] = None


class ReplacePositionRequest(AddPositionRequest):
    """替换已有持仓（旧持仓归档）。字段同新增。"""


class AddCandidateRequest(BaseModel):
    """新增候选。"""
    ts_code: str
    name: Optional[str] = None
    trigger_price: float
    trigger_direction: str = "above"
    note: str = ""
    expires_days: int = 7
    stop_advice: Optional[float] = None
    target_advice: Optional[float] = None
    strategy: Optional[str] = None


class PromoteCandidateRequest(BaseModel):
    """候选晋升为持仓（用实际成交价）。"""
    ts_code: str
    entry_price: float
    stop_loss: float
    target: float
    expires_days: int = 10
    position_size_shares: Optional[int] = None
    risk_amount: Optional[float] = None
    calibrated_confidence: Optional[float] = None
    strategy: Optional[str] = None
    regime_at_open: Optional[str] = None


class ClosePositionRequest(BaseModel):
    """平仓。postmortem=true 时串联 AI 复盘 + 校准重算。"""
    ts_code: str
    exit_price: float
    exit_reason: str = "manual"
    exit_date: Optional[str] = None
    user_notes: str = ""
    actual_fill_price: Optional[float] = None
    postmortem: bool = True


class ArchiveRequest(BaseModel):
    """归档（软删除）。section: active_positions | candidates。"""
    ts_code: str
    section: str = Field(..., pattern="^(active_positions|candidates)$")
    reason: str = "manual"
