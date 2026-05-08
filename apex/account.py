"""
账户级状态：总资金、单笔风险比例、总风险上限。

文件位置：~/.stock-journal/account.json（与 journal_dir 同目录）。

仓位计算用风险比例模型：
  shares = (total_capital * risk_per_trade_pct%) / |entry - stop_loss|
  向下取整到 100 股（A股最小一手）。
"""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import config

_TZ_CN = timezone(timedelta(hours=8))

_DEFAULT_ACCOUNT = {
    "total_capital": 100000.0,
    "risk_per_trade_pct": 1.0,
    "max_total_risk_pct": 10.0,
    "updated_at": None,
}


def _path() -> Path:
    cfg = config.get()
    journal_dir = Path(cfg["paths"]["journal_dir"]).expanduser()
    return journal_dir / "account.json"


def load() -> dict:
    """读账户配置，缺失时返回默认值副本（不写盘）。"""
    p = _path()
    if not p.exists():
        return dict(_DEFAULT_ACCOUNT)
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    for k, v in _DEFAULT_ACCOUNT.items():
        data.setdefault(k, v)
    return data


def save(data: dict) -> None:
    """写盘并更新 updated_at。"""
    out = dict(data)
    out["updated_at"] = datetime.now(_TZ_CN).isoformat(timespec="seconds")
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def update_capital(total_capital: Optional[float] = None,
                   risk_per_trade_pct: Optional[float] = None,
                   max_total_risk_pct: Optional[float] = None) -> dict:
    """局部更新账户字段。返回更新后的完整账户。"""
    acc = load()
    if total_capital is not None:
        acc["total_capital"] = float(total_capital)
    if risk_per_trade_pct is not None:
        acc["risk_per_trade_pct"] = float(risk_per_trade_pct)
    if max_total_risk_pct is not None:
        acc["max_total_risk_pct"] = float(max_total_risk_pct)
    save(acc)
    return acc


def compute_position_size(entry: float,
                          stop: float,
                          account: Optional[dict] = None,
                          risk_pct: Optional[float] = None,
                          lot_size: int = 100) -> dict:
    """
    风险比例仓位计算。

    返回 dict 字段：
      shares            : 建议手数（lot_size 整数倍）
      risk_amount       : 实际承担风险（shares × |entry-stop|）
      capital_required  : 需要资金（shares × entry）
      risk_pct_used     : 用到的 risk_pct（来自参数 / 账户）
      risk_pct_actual   : 实际风险占账户百分比
      ok                : 是否产生有效建议（false 表示无法计算或 0 股）
      warnings          : 风险/异常提示列表

    输入异常处理：
      entry 或 stop ≤ 0 → ok=false，warning 提示
      entry == stop      → ok=false，无法算 share
      entry < stop       → 仍算（按 |entry-stop|），warning 标记方向反了
      shares < 1 lot     → ok=false，warning 提示风险预算不足
      capital_required > total_capital → ok=true 但 warning 提示
    """
    if account is None:
        account = load()
    if risk_pct is None:
        risk_pct = float(account.get("risk_per_trade_pct") or 1.0)

    warnings: list[str] = []
    entry = float(entry or 0)
    stop = float(stop or 0)
    capital = float(account.get("total_capital") or 0)

    base = {
        "shares": 0,
        "risk_amount": 0.0,
        "capital_required": 0.0,
        "risk_pct_used": risk_pct,
        "risk_pct_actual": 0.0,
        "ok": False,
        "warnings": warnings,
    }

    if capital <= 0:
        warnings.append("账户总资金未设置")
        return base
    if entry <= 0 or stop <= 0:
        warnings.append("进场价或止损价缺失")
        return base
    if entry == stop:
        warnings.append("进场价 == 止损价，无法计算仓位")
        return base
    if entry < stop:
        warnings.append("止损价高于进场价（多头逻辑反了，请确认方向）")

    risk_per_share = abs(entry - stop)
    risk_budget = capital * risk_pct / 100.0
    raw_shares = risk_budget / risk_per_share
    shares = int(math.floor(raw_shares / lot_size) * lot_size)

    if shares <= 0:
        warnings.append(
            f"按 {risk_pct}% 风险预算只能买 {raw_shares:.1f} 股，不足 1 手（{lot_size} 股）。"
            "考虑放宽止损、提高风险比例、或放弃这笔交易。"
        )
        return base

    risk_amount = shares * risk_per_share
    capital_required = shares * entry

    if capital_required > capital:
        warnings.append(
            f"所需资金 {capital_required:,.0f} 超出账户总资金 {capital:,.0f}"
        )

    return {
        "shares": shares,
        "risk_amount": round(risk_amount, 2),
        "capital_required": round(capital_required, 2),
        "risk_pct_used": risk_pct,
        "risk_pct_actual": round(risk_amount / capital * 100, 3),
        "ok": True,
        "warnings": warnings,
    }


def current_total_risk(active_positions: list[dict],
                       account: Optional[dict] = None) -> dict:
    """
    汇总所有持仓的未实现风险。

    每条持仓 risk = position_size_shares × |entry_price - stop_loss|。
    缺少 size/entry/stop 的持仓计入 missing_size_count（不参与求和）。

    返回：
      total_risk_amount    : 风险金额合计
      total_risk_pct       : 占账户百分比（capital==0 时返回 None）
      max_total_risk_pct   : 配置上限（用于 UI 比对）
      over_limit           : 是否超过上限
      position_count       : 实际计入的持仓数
      missing_size_count   : 缺数据的持仓数
    """
    if account is None:
        account = load()
    capital = float(account.get("total_capital") or 0)
    max_pct = float(account.get("max_total_risk_pct") or 10.0)

    total_risk = 0.0
    counted = 0
    missing = 0
    for p in active_positions:
        shares = p.get("position_size_shares") or 0
        entry = p.get("entry_price") or 0
        stop = p.get("stop_loss") or 0
        if not shares or not entry or not stop:
            missing += 1
            continue
        total_risk += float(shares) * abs(float(entry) - float(stop))
        counted += 1

    pct = round(total_risk / capital * 100, 3) if capital > 0 else None

    return {
        "total_risk_amount": round(total_risk, 2),
        "total_risk_pct": pct,
        "max_total_risk_pct": max_pct,
        "over_limit": (pct is not None and pct > max_pct),
        "position_count": counted,
        "missing_size_count": missing,
    }
