"""AI 校准接口。"""
from __future__ import annotations

from fastapi import APIRouter

from apex import calibration as cal

router = APIRouter(prefix="/calibration", tags=["calibration"])


@router.get("")
def get_calibration():
    """读取校准表（verdict×confidence 桶胜率等）。无数据返回 null。"""
    return cal.load()


@router.post("/recompute")
def recompute():
    """重算并写入 calibration.json。"""
    return cal.compute()
