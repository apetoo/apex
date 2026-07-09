"""「我的交易系统」接口。"""
from __future__ import annotations

from fastapi import APIRouter

from apex import system

router = APIRouter(prefix="/system", tags=["system"])


@router.get("")
def get_system():
    """读取交易系统视图（Trading DNA / Behavior / AI守规 / Discipline）。无数据返回 null。"""
    return system.load()


@router.post("/recompute")
def recompute():
    """重算并写入 system.json。"""
    return system.compute()
