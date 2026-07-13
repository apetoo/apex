"""持仓 / 候选相关请求模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AddPositionRequest(BaseModel):
    """新增持仓。ts_code 会经 normalize_ts_code 规范化。"""
    ts_code: str
    name: Optional[str] = None
    entry_price: float
    stop_loss: Optional[float] = None
    target: Optional[float] = None
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
    """新增候选。ts_code 会经 normalize_ts_code 规范化。

    expires_days: None(默认) → 后端按波动率衰减动态算;
    显式传值 → 手动覆盖(保留人工干预出口)。
    """
    ts_code: str
    name: Optional[str] = None
    trigger_price: float
    trigger_direction: str = "above"
    note: str = ""
    expires_days: Optional[int] = None
    stop_advice: Optional[float] = None
    target_advice: Optional[float] = None
    strategy: Optional[str] = None
    setup: Optional[str] = None
    trigger_low: Optional[float] = None
    trigger_high: Optional[float] = None


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
    setup: Optional[str] = None
    rule_checklist: Optional[dict] = None


class ClosePositionRequest(BaseModel):
    """平仓。postmortem=true 时串联 AI 复盘 + 校准重算。"""
    ts_code: str
    exit_price: float
    exit_reason: str = "manual"
    exit_date: Optional[str] = None
    user_notes: str = ""
    actual_fill_price: Optional[float] = None
    postmortem: bool = True


class BuyRequest(BaseModel):
    """买入:开仓(新代码)或加仓(已有持仓)。每笔追加 buy trade 留痕。"""
    ts_code: str
    fill_price: float
    shares: int
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    note: str = ""
    strategy: str = "manual"


class SellRequest(BaseModel):
    """卖出:减仓或卖光。卖光自动走 close_position + postmortem + calibration。"""
    ts_code: str
    fill_price: float
    shares: int
    exit_reason: str = "manual"
    note: str = ""
    postmortem: bool = True


class ArchiveRequest(BaseModel):
    """归档（软删除）。section 缺省时后端自动探测(候选优先, 再 active_positions)。"""
    ts_code: str
    section: Optional[str] = Field(
        None, pattern="^(active_positions|candidates)$",
    )
    reason: str = "manual"


class UpdateAdviceRequest(BaseModel):
    """更新持仓止损/目标(覆盖写)。用于手动持仓同步最近 AI 分析。"""
    ts_code: str
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    calibrated_confidence: Optional[float] = None


class UpdatePositionRequest(BaseModel):
    """更新持仓交易参数(PATCH 部分覆盖, 未传/None 不动)。

    人工干预: 调止损止盈 / 重挂触发价或区间 / 改过期 / strategy / setup / 名称。
    不含 entry_price/avg_cost/shares/entry_date(录错走 buy/sell 补录, 保 trades.jsonl 口径)。
    路径 ts_code 已 normalize; body 不带 ts_code。exclude_unset 透传: 显式 null 才进入覆盖。
    """
    name: Optional[str] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    trigger_price: Optional[float] = None
    trigger_direction: Optional[str] = Field(None, pattern="^(below|above)$")
    trigger_low: Optional[float] = None
    trigger_high: Optional[float] = None
    expires_at: Optional[str] = None
    calibrated_confidence: Optional[float] = None
    strategy: Optional[str] = None
    setup: Optional[str] = None


class UpdateCandidateRequest(BaseModel):
    """更新候选交易参数(PATCH 部分覆盖, 未传/None 不动)。body 不带 ts_code。"""
    name: Optional[str] = None
    trigger_price: Optional[float] = None
    trigger_direction: Optional[str] = Field(None, pattern="^(below|above)$")
    trigger_low: Optional[float] = None
    trigger_high: Optional[float] = None
    stop_advice: Optional[float] = None
    target_advice: Optional[float] = None
    note: Optional[str] = None
    expires_at: Optional[str] = None
    strategy: Optional[str] = None
    setup: Optional[str] = None
