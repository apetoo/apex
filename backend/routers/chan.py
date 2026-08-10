"""缠论结构接口：/chan 页的 K 线 + 笔/中枢/买卖点数据。"""
from __future__ import annotations

import re

from fastapi import APIRouter, Query

from apex import chan as chan_mod
from apex import data as data_mod

router = APIRouter(prefix="/chan", tags=["chan"])

# 合法 ts_code：6 位数字 + SH/SZ/BJ 后缀（normalize 后）。非法 -> 400（QA 边界）。
_TS_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


@router.get("/{ts_code}")
def chan_structure(
    ts_code: str,
    freq: str = Query("D", description="周期: D 日 / W 周 / 30 / 60 分钟"),
    n: int = Query(250, ge=30, le=800, description="K 线根数"),
):
    """缠论结构: bars + 笔 + 中枢 + 买卖点 + 摘要。

    降级: bars<30 -> 200 + summary.reason=insufficient_bars；
    非法代码 -> 400；czsc 未装 -> 501；数据源全挂 -> 502。
    """
    code = data_mod.normalize_ts_code(ts_code)
    if not _TS_CODE_RE.match(code):
        raise ValueError(f"非法代码: {ts_code}（需 6 位数字 + 交易所后缀）")
    return chan_mod.get_structure(code, freq=freq, n=n)
