"""
VectorBT-based backtesting: treat past AI verdicts as entry signals, compute P&L.
Each journal entry gets its own Portfolio (sliced close window) to avoid signal collision.
"""
import json
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import vectorbt as vbt

from apex import config, data, journal
from apex.schemas import BULLISH_VERDICTS

# Keywords for legacy free-form verdicts (stock-analyze skill used Chinese free text)
_BULLISH_KEYWORDS = ["看多", "偏多", "多", "反弹", "建仓", "加仓"]
_BEARISH_KEYWORDS = ["看空", "偏空", "减仓", "清仓", "空"]


def _is_bullish(verdict: str) -> bool:
    if not verdict:
        return False
    if verdict in BULLISH_VERDICTS:
        return True
    # Legacy fuzzy match: contains a bullish keyword but no strong bearish keyword
    has_bull = any(kw in verdict for kw in _BULLISH_KEYWORDS)
    # Only exclude if verdict explicitly signals bearish direction
    has_bear = any(kw in verdict for kw in ["偏空", "看空"])
    return has_bull and not has_bear


def _add_days(date_str: str, n: int) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=n)
    return d.strftime("%Y-%m-%d")


def run(ts_code: Optional[str] = None, lookforward_days: Optional[int] = None) -> pd.DataFrame:
    """
    Backtest all bullish journal entries (or just one stock).
    Returns a DataFrame with one row per entry: verdict, return, hit, drawdown, etc.
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

        # Fetch price window: entry_date to entry_date + lookforward + buffer
        end_date = _add_days(entry_date, lookforward_days + 10)
        raw = data.get_daily_price(code, start_date=entry_date.replace("-", ""),
                                    end_date=end_date.replace("-", ""))
        try:
            records = json.loads(raw)
        except Exception:
            continue

        if not records or len(records) < 2:
            continue

        df = pd.DataFrame(records)
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
        df = df.set_index("trade_date").sort_index()
        close = df["close"].astype(float)

        # Trim to lookforward window
        close = close.iloc[:lookforward_days + 1]
        if len(close) < 2:
            continue

        # Entry signal on first bar; exit on last bar of window
        entries_sig = pd.Series(False, index=close.index)
        exits_sig = pd.Series(False, index=close.index)
        entries_sig.iloc[0] = True
        exits_sig.iloc[-1] = True

        # Stop/target fractions anchored to actual fill price (close.iloc[0])
        fill_price = close.iloc[0]
        price_advice = entry.get("price_advice") or {}
        stop_loss = price_advice.get("stop_loss")
        target = price_advice.get("target")
        sl_frac = abs(fill_price - stop_loss) / fill_price if stop_loss else np.nan
        tp_frac = abs(target - fill_price) / fill_price if target else np.nan

        try:
            pf = vbt.Portfolio.from_signals(
                close,
                entries=entries_sig,
                exits=exits_sig,
                init_cash=10000,
                sl_stop=sl_frac,
                tp_stop=tp_frac,
                freq="D",
            )
            total_return = float(pf.total_return())
            max_dd = float(pf.max_drawdown())
            sharpe = float(pf.sharpe_ratio()) if len(close) > 5 else None
        except Exception as e:
            total_return = (float(close.iloc[-1]) - fill_price) / fill_price
            max_dd = None
            sharpe = None

        analyzed_at = entry.get("analyzed_at", "")
        results.append({
            "ts_code": code,
            "date": entry_date,
            "analyzed_at": analyzed_at.replace("T", " ")[:16] if analyzed_at else "",
            "verdict": entry["verdict"],
            "confidence": entry.get("confidence"),
            "fill_price": round(fill_price, 2),
            "total_return": round(total_return, 4),
            "max_drawdown": round(max_dd, 4) if max_dd is not None else None,
            "sharpe": round(sharpe, 2) if sharpe is not None else None,
            "hit": total_return > 0,
            "has_features": bool(entry.get("features")),
        })

    return pd.DataFrame(results)


def print_summary(df: pd.DataFrame) -> None:
    if df.empty:
        print("无可回测的多头信号记录。")
        return

    print(f"\n{'='*50}")
    print(f"  apex 回测报告 — 多头信号 P&L")
    print(f"{'='*50}")
    print(f"  总多头信号数   : {len(df)}")
    print(f"  胜率           : {df['hit'].mean():.1%}")
    print(f"  平均收益       : {df['total_return'].mean():.2%}")
    print(f"  最大单次盈利   : {df['total_return'].max():.2%}  ({df.loc[df['total_return'].idxmax(), 'ts_code']} {df.loc[df['total_return'].idxmax(), 'date']})")
    print(f"  最大单次亏损   : {df['total_return'].min():.2%}  ({df.loc[df['total_return'].idxmin(), 'ts_code']} {df.loc[df['total_return'].idxmin(), 'date']})")
    if df["max_drawdown"].notna().any():
        print(f"  平均最大回撤   : {df['max_drawdown'].mean():.2%}")
    print(f"{'='*50}\n")

    # Per-stock summary
    if len(df["ts_code"].unique()) > 1:
        per_stock = df.groupby("ts_code").agg(
            count=("hit", "count"),
            win_rate=("hit", "mean"),
            avg_return=("total_return", "mean"),
        ).reset_index()
        per_stock["win_rate"] = per_stock["win_rate"].map("{:.0%}".format)
        per_stock["avg_return"] = per_stock["avg_return"].map("{:.2%}".format)
        print(per_stock.to_string(index=False))
        print()
