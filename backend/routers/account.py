"""账户配置与仓位计算接口。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from apex import account as account_mod
from apex import watchlist as wl

from backend.schemas.account import AccountUpdate

router = APIRouter(prefix="/account", tags=["account"])


@router.get("")
def get_account():
    """读取账户配置（总资金 / 单笔风险 / 总风险上限）。"""
    return account_mod.load()


@router.put("")
def update_account(req: AccountUpdate):
    """局部更新账户配置。"""
    return account_mod.update_capital(
        total_capital=req.total_capital,
        risk_per_trade_pct=req.risk_per_trade_pct,
        max_total_risk_pct=req.max_total_risk_pct,
    )


@router.get("/sizing")
def compute_sizing(
    entry: float = Query(..., gt=0),
    stop: float = Query(..., gt=0),
):
    """按进场价/止损价计算建议手数与风险。"""
    return account_mod.compute_position_size(entry=entry, stop=stop)


@router.get("/risk")
def total_risk():
    """当前持仓总风险监控（占账户百分比、是否超限）。"""
    acc = account_mod.load()
    positions = wl.load().get("active_positions", [])
    return account_mod.current_total_risk(positions, account=acc)


@router.get("/summary")
def account_summary():
    """账户总资产汇总: 总资产 = 总本金 + 累计已实现盈亏 + 未实现浮盈。

    现价 realtime 优先 + daily fallback(同 /api/market/prices)。取不到现价的仓位
    计入 missing_price_count, 不参与市值/浮盈求和。
    """
    acc = account_mod.load()
    positions = wl.load().get("active_positions", [])
    return account_mod.summary(positions, account=acc)
