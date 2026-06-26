"""AI 复盘接口：对已平仓记录重跑复盘。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from apex import postmortem as pm
from apex import watchlist as wl

from backend.schemas.analyze import PostmortemRunRequest

router = APIRouter(prefix="/postmortem", tags=["postmortem"])


@router.post("/run")
def run_postmortem(req: PostmortemRunRequest):
    """按 closed_at 定位已平仓记录，重跑 AI 复盘并写回。

    复盘失败返回 502（PostmortemError）；记录不存在返回 404。
    """
    records = wl.load_closed_positions()
    record = next(
        (r for r in records
         if (r.get("close") or {}).get("closed_at") == req.closed_at),
        None,
    )
    if record is None:
        raise HTTPException(status_code=404, detail=f"未找到 closed_at={req.closed_at} 的平仓记录")
    diagnosis = pm.run_and_patch(record)
    return {"diagnosis": diagnosis}
