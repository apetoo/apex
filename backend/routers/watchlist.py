"""持仓 / 候选 / 触发监控接口。"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Query

from apex import account as account_mod
from apex import calibration as cal_mod
from apex import data as data_mod
from apex import monitor as monitor_mod
from apex import postmortem as pm_mod
from apex import watchlist as wl

from backend.core.response import parse_json
from backend.schemas.common import AckRequest, TriggerCheckRequest
from backend.schemas.watchlist import (
    AddCandidateRequest,
    AddPositionRequest,
    ArchiveRequest,
    ClosePositionRequest,
    PromoteCandidateRequest,
    ReplacePositionRequest,
)

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


def _resolve_name(ts_code: str, name: Optional[str]) -> str:
    """名称缺失时反查 tushare（对齐 Streamlit 行为）。失败返回空串。"""
    if name:
        return name
    try:
        info = parse_json(data_mod.get_stock_info(ts_code=ts_code))
        if isinstance(info, list) and info:
            return info[0].get("name", "") or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


# ── 持仓 / 候选 CRUD ──────────────────────────────────────────────────────────


@router.get("")
def get_watchlist():
    """全量 watchlist：active_positions / candidates / archived。"""
    return wl.load()


@router.post("/positions")
def add_position(req: AddPositionRequest):
    """新增持仓。重复 ts_code 抛 409（带 existing）。"""
    ts_code = data_mod.normalize_ts_code(req.ts_code)
    wl.add_position(
        ts_code=ts_code,
        name=_resolve_name(ts_code, req.name),
        entry_price=req.entry_price,
        stop_loss=req.stop_loss,
        target=req.target,
        trigger_price=req.trigger_price,
        trigger_direction=req.trigger_direction,
        expires_days=req.expires_days,
        position_size_shares=req.position_size_shares,
        risk_amount=req.risk_amount,
        calibrated_confidence=req.calibrated_confidence,
        strategy=req.strategy,
    )
    return {"message": "Position added", "ts_code": ts_code}


@router.post("/positions/replace")
def replace_position(req: ReplacePositionRequest):
    """替换持仓（旧持仓归档为 archived_replaced）。"""
    ts_code = data_mod.normalize_ts_code(req.ts_code)
    wl.replace_position(
        ts_code=ts_code,
        name=_resolve_name(ts_code, req.name),
        entry_price=req.entry_price,
        stop_loss=req.stop_loss,
        target=req.target,
        trigger_price=req.trigger_price,
        trigger_direction=req.trigger_direction,
        expires_days=req.expires_days,
        position_size_shares=req.position_size_shares,
        risk_amount=req.risk_amount,
        calibrated_confidence=req.calibrated_confidence,
        strategy=req.strategy,
    )
    return {"message": "Position replaced", "ts_code": ts_code}


@router.post("/candidates")
def add_candidate(req: AddCandidateRequest):
    """新增候选（等触发）。"""
    ts_code = data_mod.normalize_ts_code(req.ts_code)
    wl.add_candidate(
        ts_code,
        _resolve_name(ts_code, req.name),
        req.trigger_price,
        trigger_direction=req.trigger_direction,
        note=req.note,
        expires_days=req.expires_days,
        stop_advice=req.stop_advice,
        target_advice=req.target_advice,
        strategy=req.strategy,
    )
    return {"message": "Candidate added", "ts_code": ts_code}


@router.post("/promote")
def promote_candidate(req: PromoteCandidateRequest):
    """候选晋升为持仓（用实际成交价）。"""
    ts_code = data_mod.normalize_ts_code(req.ts_code)
    wl.promote_candidate(
        ts_code=ts_code,
        entry_price=req.entry_price,
        stop_loss=req.stop_loss,
        target=req.target,
        expires_days=req.expires_days,
        position_size_shares=req.position_size_shares,
        risk_amount=req.risk_amount,
        calibrated_confidence=req.calibrated_confidence,
        strategy=req.strategy,
        regime_at_open=req.regime_at_open,
    )
    return {"message": "Candidate promoted", "ts_code": ts_code}


@router.post("/close")
def close_position(req: ClosePositionRequest):
    """平仓。postmortem=true 时串联 AI 复盘 + 校准重算（对齐 Streamlit 平仓流程）。"""
    ts_code = data_mod.normalize_ts_code(req.ts_code)
    rec = wl.close_position(
        ts_code=ts_code,
        exit_price=req.exit_price,
        exit_reason=req.exit_reason,
        exit_date=req.exit_date,
        user_notes=req.user_notes,
        actual_fill_price=req.actual_fill_price,
    )
    diagnosis = None
    if req.postmortem:
        diagnosis = pm_mod.run_and_patch(rec)
        try:
            cal_mod.compute()
        except Exception:  # noqa: BLE001 — 校准失败不阻断平仓响应
            pass
    return {"message": "Position closed", "record": rec, "diagnosis": diagnosis}


@router.post("/archive")
def archive_entry(req: ArchiveRequest):
    """归档（软删除，写 status=archived_<reason>）。"""
    ts_code = data_mod.normalize_ts_code(req.ts_code)
    moved = wl.archive_entry(ts_code=ts_code, section=req.section, reason=req.reason)
    return {"message": "Entry archived" if moved else "Entry not found", "moved": moved}


@router.get("/closed")
def get_closed_positions(
    limit: Optional[int] = Query(None, ge=1, le=500),
    since_days: Optional[int] = Query(None, ge=1, le=3650),
):
    """已平仓记录（按 closed_at 倒序）。"""
    return wl.load_closed_positions(limit=limit, since_days=since_days)


@router.post("/migrate")
def migrate():
    """一次性迁移：规范 ts_code、补全名称、合并 legacy 日志、去重持仓。"""
    return wl.migrate_and_backfill()


# ── 触发监控 ──────────────────────────────────────────────────────────────────
# 独立 prefix，便于前端按职能归类。


trigger_router = APIRouter(prefix="/triggers", tags=["watchlist"])


@trigger_router.get("")
def list_triggers(
    hours: int = Query(24, ge=1, le=720),
    include_acked: bool = Query(False),
):
    """近 N 小时触发记录（默认过滤已读，最新在前）。"""
    return monitor_mod.load_recent_triggers(hours=hours, include_acked=include_acked)


@trigger_router.post("/check")
def check_triggers(req: TriggerCheckRequest):
    """跑一次触发检查。可传 prices 复用行情避免重复拉取。"""
    return monitor_mod.check_once(notify_enabled=req.notify_enabled, prices=req.prices)


@trigger_router.post("/ack")
def ack_triggers(req: AckRequest):
    """标记触发已读。支持单个 signature 或批量 signatures。"""
    sigs = req.signatures or ([req.signature] if req.signature else [])
    for sig in sigs:
        monitor_mod.ack_trigger(sig)
    return {"message": "Triggers acknowledged", "count": len(sigs)}
