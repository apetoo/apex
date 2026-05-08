"""
Backtest module — two modes:

1. run_realized(): 实际平仓分析 — 直接读 closed_positions.jsonl，无模拟，最可信。
2. run():          信号模拟回测 — 以 AI verdict 为入场信号，T+1 建模 + 真实成本 + 基准对比。

A股成本模型:
  佣金 万2.5（双向）+ 印花税 千1（卖出） → 合计约 0.15% 单程来回。
T+1 建模:
  信号日为 bar[0]，bar[1] 收盘价作为实际入场价代理（无法用次日开盘，用收盘作保守估计）。
"""
import json
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import vectorbt as vbt

from apex import config, data, journal
from apex.schemas import BULLISH_VERDICTS

_COMMISSION_RATE = 0.00025   # 万2.5 双向佣金
_STAMP_DUTY_RATE = 0.001     # 千1 印花税（卖出）
_ROUND_TRIP_COST = _COMMISSION_RATE * 2 + _STAMP_DUTY_RATE  # ≈ 0.0015

_BENCHMARK_CODE = "000300.SH"

_BULLISH_KEYWORDS = ["看多", "偏多", "多", "反弹", "建仓", "加仓"]


def _is_bullish(verdict: str) -> bool:
    if not verdict:
        return False
    if verdict in BULLISH_VERDICTS:
        return True
    has_bull = any(kw in verdict for kw in _BULLISH_KEYWORDS)
    has_bear = any(kw in verdict for kw in ["偏空", "看空"])
    return has_bull and not has_bear


def _add_days(date_str: str, n: int) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=n)
    return d.strftime("%Y-%m-%d")


def _apply_costs(gross_return: float) -> float:
    """Deduct A-share round-trip costs: commission both ways + stamp duty on sell."""
    return gross_return - _ROUND_TRIP_COST


_ts_pro = None


def _tushare_pro():
    global _ts_pro
    if _ts_pro is None:
        import tushare as ts
        cfg = config.get()
        ts.set_token(cfg["tushare"]["token"])
        _ts_pro = ts.pro_api()
    return _ts_pro


def _benchmark_return(entry_date: str, exit_date: str) -> Optional[float]:
    """hs300 close-to-close return for [entry_date, exit_date] window. None on failure."""
    try:
        pro = _tushare_pro()
        df = pro.index_daily(
            ts_code=_BENCHMARK_CODE,
            start_date=entry_date.replace("-", ""),
            end_date=exit_date.replace("-", ""),
            fields="trade_date,close",
        )
        if df is None or len(df) < 2:
            return None
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
        df = df.sort_values("trade_date").reset_index(drop=True)
        c0 = float(df["close"].iloc[0])
        c1 = float(df["close"].iloc[-1])
        return (c1 - c0) / c0 if c0 > 0 else None
    except Exception:
        return None


# ── 实际平仓分析 ─────────────────────────────────────────────────────────────

def run_realized(closed: Optional[list] = None) -> pd.DataFrame:
    """
    Analyse actual closed positions — no simulation.
    Uses real fill/exit prices from closed_positions.jsonl.
    Returns one row per closed trade with cost-adjusted returns.
    """
    from apex import watchlist as wl_mod
    if closed is None:
        closed = wl_mod.load_closed_positions()
    if not closed:
        return pd.DataFrame()

    rows = []
    for rec in closed:
        o = rec.get("open") or {}
        c = rec.get("close") or {}

        fill = float(o.get("actual_fill_price") or o.get("entry_price") or 0)
        exit_ = float(c.get("actual_exit_price") or 0)
        gross = c.get("realized_pnl_pct")
        if gross is None and fill > 0 and exit_ > 0:
            gross = (exit_ - fill) / fill
        net = round(gross - _ROUND_TRIP_COST, 4) if gross is not None else None

        rows.append({
            "ts_code": rec.get("ts_code", ""),
            "name": rec.get("name", ""),
            "strategy": o.get("strategy") or "unknown",
            "regime": o.get("regime_at_open") or "unknown",
            "entry_date": o.get("entry_date", ""),
            "exit_date": c.get("exit_date", ""),
            "exit_reason": c.get("exit_reason", ""),
            "days_held": int(c.get("days_held") or 0),
            "fill_price": fill or None,
            "exit_price": exit_ or None,
            "gross_pnl_pct": round(gross, 4) if gross is not None else None,
            "net_pnl_pct": net,
            "hit": net > 0 if net is not None else None,
            "ai_verdict": o.get("ai_verdict") or "",
            "ai_confidence": o.get("ai_confidence"),
        })

    return pd.DataFrame(rows)


# ── 信号模拟回测 ──────────────────────────────────────────────────────────────

def run(ts_code: Optional[str] = None,
        lookforward_days: Optional[int] = None,
        include_benchmark: bool = True) -> pd.DataFrame:
    """
    Simulate P&L of bullish journal entries.
    T+1 模型：信号日 bar[0]，bar[1] 收盘入场；bar[-1] 出场。
    Returns one row per entry.
    """
    cfg = config.get()
    if lookforward_days is None:
        lookforward_days = cfg["backtest"]["lookforward_days"]
    min_entries = cfg["backtest"]["min_entries_for_analysis"]

    entries = journal.load_entries(ts_code=ts_code)
    long_entries = [e for e in entries if _is_bullish(e.get("verdict", ""))]

    if len(long_entries) < min_entries:
        print(f"⚠ 多头信号只有 {len(long_entries)} 条，少于最小要求 {min_entries}，仍继续但结果仅供参考。")

    results = []
    for entry in long_entries:
        code = entry["ts_code"]
        entry_date = entry.get("date", "")
        if not entry_date:
            continue

        end_date = _add_days(entry_date, lookforward_days + 10)
        raw = data.get_daily_price(code,
                                   start_date=entry_date.replace("-", ""),
                                   end_date=end_date.replace("-", ""))
        try:
            records = json.loads(raw)
        except Exception:
            continue

        if not records or len(records) < 3:
            continue

        df = pd.DataFrame(records)
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
        df = df.set_index("trade_date").sort_index()
        close = df["close"].astype(float)

        close = close.iloc[:lookforward_days + 1]
        if len(close) < 3:  # need signal bar + T+1 entry + at least one exit
            continue

        entries_sig = pd.Series(False, index=close.index)
        exits_sig = pd.Series(False, index=close.index)
        entries_sig.iloc[1] = True   # T+1 入场
        exits_sig.iloc[-1] = True

        fill_price = close.iloc[1]   # T+1 收盘价作为入场价代理
        exit_date = close.index[-1].strftime("%Y-%m-%d")

        price_advice = entry.get("price_advice") or {}
        stop_loss = price_advice.get("stop_loss")
        target = price_advice.get("target")
        sl_frac = abs(fill_price - stop_loss) / fill_price if stop_loss and fill_price > 0 else np.nan
        tp_frac = abs(target - fill_price) / fill_price if target and fill_price > 0 else np.nan

        total_return = None
        max_dd = None
        sharpe = None
        try:
            pf = vbt.Portfolio.from_signals(
                close,
                entries=entries_sig,
                exits=exits_sig,
                init_cash=10000,
                sl_stop=sl_frac if not np.isnan(sl_frac) else None,
                tp_stop=tp_frac if not np.isnan(tp_frac) else None,
                freq="D",
            )
            total_return = _apply_costs(float(pf.total_return()))
            max_dd = float(pf.max_drawdown())
            sharpe = float(pf.sharpe_ratio()) if len(close) > 5 else None
        except Exception:
            if fill_price > 0:
                gross = (float(close.iloc[-1]) - fill_price) / fill_price
                total_return = _apply_costs(gross)

        if total_return is None:
            continue

        bench = _benchmark_return(entry_date, exit_date) if include_benchmark else None
        excess = round(total_return - bench, 4) if bench is not None else None

        analyzed_at = entry.get("analyzed_at", "")
        results.append({
            "ts_code": code,
            "date": entry_date,
            "analyzed_at": analyzed_at.replace("T", " ")[:16] if analyzed_at else "",
            "verdict": entry["verdict"],
            "confidence": entry.get("confidence"),
            "strategy": entry.get("strategy") or "",
            "fill_price": round(fill_price, 2),
            "net_return": round(total_return, 4),
            "benchmark_return": round(bench, 4) if bench is not None else None,
            "excess_return": excess,
            "max_drawdown": round(max_dd, 4) if max_dd is not None else None,
            "sharpe": round(sharpe, 2) if sharpe is not None else None,
            "hit": total_return > 0,
            "beat_benchmark": (excess or 0) > 0 if bench is not None else None,
            "has_features": bool(entry.get("features")),
        })

    return pd.DataFrame(results)


def print_summary(df: pd.DataFrame) -> None:
    if df.empty:
        print("无可回测的多头信号记录。")
        return

    col = "net_return" if "net_return" in df.columns else "total_return"
    print(f"\n{'='*50}")
    print(f"  apex 回测报告 — 多头信号 P&L（含成本）")
    print(f"{'='*50}")
    print(f"  总多头信号数   : {len(df)}")
    print(f"  胜率           : {df['hit'].mean():.1%}")
    print(f"  平均净收益     : {df[col].mean():.2%}")
    if "benchmark_return" in df.columns and df["benchmark_return"].notna().any():
        bench_mean = df["benchmark_return"].mean()
        print(f"  基准平均收益   : {bench_mean:.2%}")
        print(f"  平均超额收益   : {(df[col].mean() - bench_mean):.2%}")
    best_idx = df[col].idxmax()
    worst_idx = df[col].idxmin()
    print(f"  最大单次盈利   : {df.loc[best_idx, col]:.2%}  ({df.loc[best_idx, 'ts_code']} {df.loc[best_idx, 'date']})")
    print(f"  最大单次亏损   : {df.loc[worst_idx, col]:.2%}  ({df.loc[worst_idx, 'ts_code']} {df.loc[worst_idx, 'date']})")
    if df["max_drawdown"].notna().any():
        print(f"  平均最大回撤   : {df['max_drawdown'].mean():.2%}")
    print(f"{'='*50}\n")
