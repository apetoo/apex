"""
Backtest module — 四种视图:

1. run_realized():    实际平仓分析 — 直接读 closed_positions.jsonl，无模拟，最可信。
2. run():             信号模拟回测 — 以 AI verdict 为入场信号，逐笔 P&L（保持旧契约：只返回可成交行）。
3. run_sweep():       持有期扫描 — 每条信号 × 多档持有期，回答"最优持有几天"。
4. aggregate():       校准切片 — 对 run() 的逐笔结果按 置信度桶/verdict/source 聚合，回答"AI 自信时准不准"。
5. run_portfolio():   组合级净值 — 全部信号喂进单个 vectorbt Portfolio（共享资金池），产出净值曲线。

A股成本模型（可配, config.backtest.costs）:
  佣金 万2.5（双向）+ 印花税 千1（卖出） → 来回约 0.15%。
T+1 真实入场建模:
  信号日为 bar[0]，bar[1] **开盘价**入场（涨停封板/一字板 → unfillable，剔除出胜率分母并单列计数）。
  基准对比窗口用实际 fill_date（非信号日），消除固定方向偏置。
"""
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import vectorbt as vbt

from apex import config, data, journal, market_cache
from apex.schemas import BULLISH_VERDICTS

_BENCHMARK_CODE = "000300.SH"

_BULLISH_KEYWORDS = ["看多", "偏多", "多", "反弹", "建仓", "加仓"]

# sharpe 需要足够样本才有统计意义；短窗口(<20 bar)直接返回 None，避免误导
_SHARPE_MIN_BARS = 20


# ── 配置 & 成本 ───────────────────────────────────────────────────────────────

def _costs() -> tuple[float, float, float]:
    """从 config 读成本率；缺失时回退默认（万2.5双向佣金 + 千1印花税）。"""
    cfg = config.get()
    c = (cfg.get("backtest") or {}).get("costs") or {}
    commission = float(c.get("commission_rate", 0.00025))
    stamp = float(c.get("stamp_duty_rate", 0.001))
    return commission, stamp, commission * 2 + stamp


def _round_trip_cost() -> float:
    return _costs()[2]


def _apply_costs(gross_return: float) -> float:
    """Deduct A-share round-trip costs: commission both ways + stamp duty on sell."""
    return gross_return - _round_trip_cost()


def _limit_pct(ts_code: str) -> float:
    """按板块返回涨停幅度。30/68→20%（创业/科创），8/4→30%（北交），其余 10%。"""
    code = ts_code.split(".")[0]
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("8", "4")):
        return 0.30
    return 0.10


# ── 基础工具 ──────────────────────────────────────────────────────────────────

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
    """hs300 close-to-close return for [entry_date, exit_date] window. None on failure.

    注意：entry_date 应传**实际入场日**(fill_date, T+1)，不是信号日，否则基准错位一天。
    """
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


def _fetch_daily(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """拉单票日线（market_cache parquet 缓存为主, akshare 兜底），不算 MA、不截断 tail(60)。

    供 run_portfolio 兜底（主路径走 _build_ohlc_panels 的批量拉取）。
    返回 DataFrame, index=trade_date(datetime), cols: open/high/low/close/vol。失败返回空。
    """
    s = start_date.replace("-", "")
    e = end_date.replace("-", "")
    try:
        df = market_cache.load_daily_full(code, start_date=s, end_date=e, adj="qfq")
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    # 兜底：akshare（market_cache 对停牌/无数据返空时）
    try:
        import akshare as ak
        symbol = code.split(".")[0]
        df = ak.stock_zh_a_hist(symbol=symbol, start_date=s, end_date=e,
                                 adjust="qfq")
        df = df.rename(columns={"日期": "trade_date", "开盘": "open",
                                "收盘": "close", "最高": "high",
                                "最低": "low", "成交量": "vol"})
        df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str)
                                          .str.replace("-", ""), format="%Y%m%d")
        return df.set_index("trade_date").sort_index()
    except Exception:
        return pd.DataFrame()


def _tushare():
    """Ensure tushare token is set (mirrors data._tushare)."""
    cfg = config.get()
    import tushare as ts
    ts.set_token(cfg["tushare"]["token"])


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
        net = round(gross - _round_trip_cost(), 4) if gross is not None else None

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


# ── 信号模拟回测：内核 ────────────────────────────────────────────────────────

def _simulate_one(entry: dict, bars: pd.DataFrame, holding_period: int,
                  include_benchmark: bool) -> Optional[dict]:
    """单条信号 × 单持有期 → 一行结果。

    bars: DataFrame, index=trade_date(datetime, 升序), 需含 open/high/low/close。
          取 [:holding_period+1] 作为窗口（信号日 + T+1 入场 + 出场）。
    返回结果 dict；数据不足(<3 bar)返回 None。
    unfillable（涨停买不进）返回带 unfillable=True 的行，net_return=None。
    """
    code = entry["ts_code"]
    # 窗口 = 信号日(bar0) + T+1 入场(bar1) + 持有 N 天 → 出场(bar N+1)，共 N+2 根。
    window = bars.iloc[:holding_period + 2]
    if len(window) < 3:  # 至少 信号日 + 入场 + 出场
        return None

    prev_close = float(window["close"].iloc[0])
    t1_open = float(window["open"].iloc[1])
    t1_high = float(window["high"].iloc[1])
    t1_low = float(window["low"].iloc[1])
    t1_close = float(window["close"].iloc[1])

    limit = _limit_pct(code)
    limit_up_price = prev_close * (1 + limit)
    # 一字板（全天封涨停）或开盘即涨停 → 买不进
    unfillable = (t1_high == t1_low == t1_close) or \
                 (t1_open >= limit_up_price - 1e-4 and t1_high >= limit_up_price - 1e-4)

    fill_price = t1_open
    fill_date = window.index[1].strftime("%Y-%m-%d")
    exit_date = window.index[-1].strftime("%Y-%m-%d")

    base = {
        "ts_code": code,
        "date": entry.get("date", ""),
        "analyzed_at": (entry.get("analyzed_at", "") or "").replace("T", " ")[:16],
        "verdict": entry["verdict"],
        "confidence": entry.get("confidence"),
        "calibrated_confidence": entry.get("calibrated_confidence"),
        "strategy": entry.get("strategy") or entry.get("source") or "unknown",
        "fill_price": round(fill_price, 2),
        "fill_date": fill_date,
        "exit_date": exit_date,
        "holding_period": holding_period,
        "has_features": bool(entry.get("features")),
        "unfillable": False,
    }

    if unfillable:
        base.update({
            "net_return": None,
            "benchmark_return": None,
            "excess_return": None,
            "max_drawdown": None,
            "sharpe": None,
            "hit": None,
            "beat_benchmark": None,
            "unfillable": True,
        })
        return base

    close = window["close"].astype(float)
    open_ = window["open"].astype(float)
    high_ = window["high"].astype(float)
    low_ = window["low"].astype(float)
    entries_sig = pd.Series(False, index=close.index)
    exits_sig = pd.Series(False, index=close.index)
    entries_sig.iloc[1] = True   # T+1 入场
    exits_sig.iloc[-1] = True

    price_advice = entry.get("price_advice") or {}
    stop_loss = price_advice.get("stop_loss")
    target = price_advice.get("target")
    sl_frac = abs(fill_price - stop_loss) / fill_price if stop_loss and fill_price > 0 else np.nan
    tp_frac = abs(target - fill_price) / fill_price if target and fill_price > 0 else np.nan

    # fill 模型：entry @ T+1 open、exit @ 末根 close；SL/TP 用 high/low intrabar 触发。
    # vbt 的 price 决定订单成交价——构造 entry bar=open、其余=close 的 series 即可分别定价。
    fill_price_series = close.copy()
    fill_price_series.iloc[1] = float(open_.iloc[1])

    total_return = None
    max_dd = None
    sharpe = None
    try:
        pf = vbt.Portfolio.from_signals(
            close=close, open=open_, high=high_, low=low_,
            price=fill_price_series,
            entries=entries_sig,
            exits=exits_sig,
            init_cash=10000,
            sl_stop=sl_frac if not np.isnan(sl_frac) else None,
            tp_stop=tp_frac if not np.isnan(tp_frac) else None,
            freq="D",
        )
        total_return = _apply_costs(float(pf.total_return()))
        max_dd = float(pf.max_drawdown())
        if len(close) > _SHARPE_MIN_BARS:
            sharpe = float(pf.sharpe_ratio())
    except Exception:
        if fill_price > 0:
            gross = (float(close.iloc[-1]) - fill_price) / fill_price
            total_return = _apply_costs(gross)

    if total_return is None:
        return None

    bench = _benchmark_return(fill_date, exit_date) if include_benchmark else None
    excess = round(total_return - bench, 4) if bench is not None else None

    base.update({
        "net_return": round(total_return, 4),
        "benchmark_return": round(bench, 4) if bench is not None else None,
        "excess_return": excess,
        "max_drawdown": round(max_dd, 4) if max_dd is not None else None,
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "hit": total_return > 0,
        "beat_benchmark": (excess or 0) > 0 if bench is not None else None,
    })
    return base


def _load_bars(code: str, entry_date: str, holding_period: int) -> pd.DataFrame:
    """拉一条信号所需的日线窗口（走 market_cache parquet 缓存, qfq 复权）。

    返回 DataFrame, index=trade_date(datetime 升序), 含 open/high/low/close。失败/形状错返回空。
    语义与旧 get_daily_price 一致：从 entry_date 起升序，_simulate_one 取 iloc[:hp+2]。
    """
    end_date = _add_days(entry_date, holding_period + 20)
    try:
        df = market_cache.load_daily_full(
            code,
            start_date=entry_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adj="qfq",
        )
    except Exception as exc:
        print(f"  ⚠ {code} {entry_date} 拉行情失败: {type(exc).__name__}: {exc}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def _prefetch_signals(codes, entries, holding_period: int) -> None:
    """批量预热所有信号的日线 parquet（按交易日横截面，调用数与 code 数解耦）。

    一次 load_raw_daily_batch 覆盖所有 code 的全局窗口（调用数 = 交易日×2，远低于 per-code
    的 N×2），预热后 _load_bars→load_daily_full 命中 parquet、0 网络。失败静默（_load_bars
    各自兜底）。
    """
    dates = [e.get("date", "") for e in entries if e.get("date")]
    if not dates or not codes:
        return
    start = min(dates).replace("-", "")
    end = _add_days(max(dates), holding_period + 20).replace("-", "")
    try:
        market_cache.load_raw_daily_batch(list(codes), start, end)
    except Exception as e:
        print(f"⚠ 回测预热批量拉取失败: {type(e).__name__}: {e}")


def _run_entries(long_entries: list, holding_period: int,
                 include_benchmark: bool) -> tuple[list, int]:
    """对一批多头信号逐条模拟，返回 (所有行含 unfillable, unfillable_count)。

    既有 fillable 也有 unfillable 行（unfillable 行 net_return=None）。
    run() 只取 fillable；aggregate/sweep 各取所需。
    """
    rows = []
    unfillable_count = 0
    for entry in long_entries:
        entry_date = entry.get("date", "")
        if not entry_date:
            continue
        bars = _load_bars(entry["ts_code"], entry_date, holding_period)
        if bars.empty or len(bars) < 3:
            continue
        row = _simulate_one(entry, bars, holding_period, include_benchmark)
        if row is None:
            continue
        if row.get("unfillable"):
            unfillable_count += 1
        rows.append(row)
    return rows, unfillable_count


# ── 信号模拟回测：逐笔（保持旧契约） ──────────────────────────────────────────

def run(ts_code: Optional[str] = None,
        lookforward_days: Optional[int] = None,
        include_benchmark: bool = True) -> pd.DataFrame:
    """
    Simulate P&L of bullish journal entries.
    T+1 开盘入场，持有 lookforward_days 出场。Returns one row per **fillable** entry.

    契约不变：只返回可成交行（net_return 恒为 float），unfillable 不进逐笔表
    （无法成交，计入 aggregate/sweep 的 unfillable_count）。
    """
    cfg = config.get()
    if lookforward_days is None:
        lookforward_days = cfg["backtest"]["lookforward_days"]
    min_entries = cfg["backtest"]["min_entries_for_analysis"]

    entries = journal.load_entries(ts_code=ts_code)
    long_entries = [e for e in entries if _is_bullish(e.get("verdict", ""))]

    if len(long_entries) < min_entries:
        print(f"⚠ 多头信号只有 {len(long_entries)} 条，少于最小要求 {min_entries}，仍继续但结果仅供参考。")

    codes = sorted({e["ts_code"] for e in long_entries if e.get("ts_code")})
    _prefetch_signals(codes, long_entries, lookforward_days)

    all_rows, _ = _run_entries(long_entries, lookforward_days, include_benchmark)
    fillable = [r for r in all_rows if not r.get("unfillable")]
    return pd.DataFrame(fillable)


# ── P2: 持有期扫描 ────────────────────────────────────────────────────────────

def run_sweep(ts_code: Optional[str] = None,
              holding_periods: Optional[list] = None,
              include_benchmark: bool = True) -> dict:
    """每条信号 × 每档持有期 → per_signal 扁平行 + by_period 聚合。

    by_period 每项: {holding_period, n, fillable_n, unfillable_count,
                     win_rate, avg_net_return, avg_max_dd, avg_excess_return}
    回答"AI 信号最优持有几天"。
    """
    cfg = config.get()
    if holding_periods is None:
        holding_periods = (cfg.get("backtest") or {}).get("holding_periods") or [1, 3, 5, 10, 20]
    holding_periods = sorted(set(int(p) for p in holding_periods))
    max_hp = max(holding_periods)

    entries = journal.load_entries(ts_code=ts_code)
    long_entries = [e for e in entries if _is_bullish(e.get("verdict", ""))]

    codes = sorted({e["ts_code"] for e in long_entries if e.get("ts_code")})
    _prefetch_signals(codes, long_entries, max_hp)

    per_signal = []
    # by_period 聚合
    agg: dict = {hp: {"n": 0, "fillable_n": 0, "unfillable_count": 0,
                      "wins": 0, "net_sum": 0.0, "dd_sum": 0.0, "dd_n": 0,
                      "excess_sum": 0.0, "excess_n": 0}
                 for hp in holding_periods}

    # 按 entry 缓存 bars（拉到 max_hp 窗口，各 hp 复用切片）
    for entry in long_entries:
        entry_date = entry.get("date", "")
        if not entry_date:
            continue
        bars = _load_bars(entry["ts_code"], entry_date, max_hp)
        if bars.empty or len(bars) < 3:
            continue
        for hp in holding_periods:
            row = _simulate_one(entry, bars, hp, include_benchmark)
            if row is None:
                continue
            per_signal.append(row)
            a = agg[hp]
            a["n"] += 1
            if row.get("unfillable"):
                a["unfillable_count"] += 1
                continue
            a["fillable_n"] += 1
            if row.get("hit"):
                a["wins"] += 1
            a["net_sum"] += row["net_return"] or 0.0
            if row.get("max_drawdown") is not None:
                a["dd_sum"] += row["max_drawdown"]
                a["dd_n"] += 1
            if row.get("excess_return") is not None:
                a["excess_sum"] += row["excess_return"]
                a["excess_n"] += 1

    by_period = []
    for hp in holding_periods:
        a = agg[hp]
        fn = a["fillable_n"]
        by_period.append({
            "holding_period": hp,
            "n": a["n"],
            "fillable_n": fn,
            "unfillable_count": a["unfillable_count"],
            "win_rate": round(a["wins"] / fn, 4) if fn else None,
            "avg_net_return": round(a["net_sum"] / fn, 4) if fn else None,
            "avg_max_drawdown": round(a["dd_sum"] / a["dd_n"], 4) if a["dd_n"] else None,
            "avg_excess_return": round(a["excess_sum"] / a["excess_n"], 4) if a["excess_n"] else None,
        })

    return {"per_signal": per_signal, "by_period": by_period}


# ── P3: 校准切片 ──────────────────────────────────────────────────────────────

def _conf_bucket(conf) -> Optional[str]:
    """置信度 → 1-3/4-6/7-10 桶（复用 calibration._bucket_for 口径）。None→None。"""
    if conf is None:
        return None
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return None
    if c <= 3:
        return "1-3"
    if c <= 6:
        return "4-6"
    return "7-10"


def _slice_bucket(rows: list, key_fn) -> list:
    """对 fillable 行按 key_fn 分桶聚合。返回 [{key, n, win_rate, avg_net_return, avg_excess_return}]。"""
    groups: dict = {}
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        g = groups.setdefault(k, {"n": 0, "wins": 0, "net_sum": 0.0,
                                  "excess_sum": 0.0, "excess_n": 0})
        g["n"] += 1
        if r.get("hit"):
            g["wins"] += 1
        g["net_sum"] += r.get("net_return") or 0.0
        if r.get("excess_return") is not None:
            g["excess_sum"] += r["excess_return"]
            g["excess_n"] += 1
    out = []
    for k in sorted(groups.keys()):
        g = groups[k]
        out.append({
            "key": k,
            "n": g["n"],
            "win_rate": round(g["wins"] / g["n"], 4) if g["n"] else None,
            "avg_net_return": round(g["net_sum"] / g["n"], 4) if g["n"] else None,
            "avg_excess_return": round(g["excess_sum"] / g["excess_n"], 4) if g["excess_n"] else None,
        })
    return out


def aggregate(ts_code: Optional[str] = None,
              lookforward_days: Optional[int] = None,
              include_benchmark: bool = True) -> dict:
    """对 run() 的逐笔回测做切片聚合，回答"AI 自信时准不准"。

    优先用 calibrated_confidence 分桶（系统真正想验证的校准后置信度），回退 confidence。
    输出: by_confidence_bucket / by_verdict / by_strategy(source) + unfillable 统计。
    互补于 calibration.compute()（后者聚合真实平仓，本函数聚合模拟回测，样本量大）。
    """
    cfg = config.get()
    if lookforward_days is None:
        lookforward_days = cfg["backtest"]["lookforward_days"]

    entries = journal.load_entries(ts_code=ts_code)
    long_entries = [e for e in entries if _is_bullish(e.get("verdict", ""))]

    codes = sorted({e["ts_code"] for e in long_entries if e.get("ts_code")})
    _prefetch_signals(codes, long_entries, lookforward_days)

    all_rows, unfillable_count = _run_entries(long_entries, lookforward_days, include_benchmark)
    fillable = [r for r in all_rows if not r.get("unfillable")]

    def _conf_key(r):
        return _conf_bucket(r.get("calibrated_confidence")) or _conf_bucket(r.get("confidence"))

    return {
        "lookforward_days": lookforward_days,
        "total_signals": len(all_rows),
        "fillable_count": len(fillable),
        "unfillable_count": unfillable_count,
        "by_confidence_bucket": _slice_bucket(fillable, _conf_key),
        "by_verdict": _slice_bucket(fillable, lambda r: r.get("verdict")),
        "by_strategy": _slice_bucket(fillable, lambda r: r.get("strategy") or "unknown"),
    }


# ── P4: 组合级净值 ────────────────────────────────────────────────────────────

def _build_ohlc_panels(codes: list, entries: list, holding_period: int) -> dict:
    """拉所有 code 的日线，拼成 wide OHLC 面板（index=交易日并集, cols=code）。

    走 market_cache 批量拉取（按交易日横截面，调用数=交易日×2，与 code 数解耦），
    parquet 缓存 + 限频重试。批量缺的 code 用 _fetch_daily 单票 akshare 兜底。停牌缺口 ffill。
    返回 {"open":df,"high":df,"low":df,"close":df}；任一为空 → 全部空 DataFrame。
    """
    empty = {k: pd.DataFrame() for k in ("open", "high", "low", "close")}
    if not entries:
        return empty
    dates = [e.get("date", "") for e in entries if e.get("date")]
    if not dates:
        return empty
    start = min(dates)
    end = _add_days(max(dates), holding_period + 10)

    # 批量按交易日横截面拉取（一次覆盖所有 code，调用数与 code 数解耦）
    try:
        batch = market_cache.load_raw_daily_batch(
            list(codes), start.replace("-", ""), end.replace("-", ""),
        )
    except Exception as e:
        print(f"⚠ portfolio 批量拉取失败: {type(e).__name__}: {e}")
        batch = {}

    cols = {k: {} for k in ("open", "high", "low", "close")}
    for code in codes:
        df = batch.get(code)
        if df is None or df.empty:
            # 兜底：单票（market_cache 对停牌/无数据返空时走 akshare）
            df = _fetch_daily(code, start, end)
        if df is None or df.empty:
            continue
        if "adj_factor" in df.columns:
            df = market_cache._apply_adj(df, "qfq")
        for k in cols:
            cols[k][code] = df[k].astype(float)
    if not cols["close"]:
        return empty
    panels = {k: pd.DataFrame(v).sort_index().ffill(limit=30) for k, v in cols.items()}
    # 对齐到 close 面板的 index/cols
    ref = panels["close"]
    panels = {k: p.reindex(index=ref.index, columns=ref.columns) for k, p in panels.items()}
    return panels


def _build_signals(entries: list, price_index: pd.DatetimeIndex,
                   holding_period: int) -> tuple[pd.DataFrame, pd.DataFrame, list]:
    """构造 wide entries/exits 信号矩阵，并对同 code 重复信号做"仅空仓入场"去重。

    返回 (entries_df, exits_df, trades_meta)。trades_meta 为每笔计划入场元信息。
    """
    codes = sorted({e["ts_code"] for e in entries})
    entries_df = pd.DataFrame(False, index=price_index, columns=codes)
    exits_df = pd.DataFrame(False, index=price_index, columns=codes)
    trades_meta = []
    # 按 code 记录每笔持仓的 [fill_idx, exit_idx)，新信号若落入已有持仓区间则跳过
    open_until: dict = {}  # code → 当前持仓的 exit_idx（exclusive）

    # 按信号日排序
    for entry in sorted(entries, key=lambda e: e.get("date", "")):
        code = entry["ts_code"]
        sig_date = entry.get("date", "")
        if not sig_date:
            continue
        sig_ts = pd.to_datetime(sig_date)
        # 找信号日之后的第一个交易日（T+1 入场）
        future = price_index[price_index > sig_ts]
        if len(future) < 1:
            continue
        fill_idx = future[0]
        fill_pos = price_index.get_loc(fill_idx)
        exit_pos = fill_pos + holding_period
        if exit_pos >= len(price_index):
            exit_pos = len(price_index) - 1
        # 仅空仓入场：若该 code 当前已在持仓，跳过
        if code in open_until and open_until[code] > fill_pos:
            continue
        entries_df.loc[fill_idx, code] = True
        exit_ts = price_index[exit_pos]
        exits_df.loc[exit_ts, code] = True
        open_until[code] = exit_pos + 1
        trades_meta.append({
            "ts_code": code,
            "fill_date": fill_idx.strftime("%Y-%m-%d"),
            "exit_date": exit_ts.strftime("%Y-%m-%d"),
            "verdict": entry.get("verdict"),
            "confidence": entry.get("confidence"),
            "holding_period": holding_period,
        })
    return entries_df, exits_df, trades_meta


def run_portfolio(ts_code: Optional[str] = None,
                  lookforward_days: Optional[int] = None) -> dict:
    """全部多头信号喂进单个 vectorbt Portfolio（共享资金池）→ 单条净值曲线。

    现金共享 + 按信号顺序仅空仓入场 + 固定持有期出场 + 佣金（印花税暂略，见注释）。
    这正是前端 ED11 注释里"想要但客户端累乘做不对"的组合级净值——并发持仓的资金占用/
    复利必须服务端用 vectorbt 算，不能客户端 ∏(1+r)。
    """
    cfg = config.get()
    if lookforward_days is None:
        lookforward_days = cfg["backtest"]["lookforward_days"]
    commission, _stamp, _rtc = _costs()

    entries = journal.load_entries(ts_code=ts_code)
    long_entries = [e for e in entries if _is_bullish(e.get("verdict", ""))]
    if not long_entries:
        return {"equity_curve": [], "stats": {}, "trades": []}

    codes = sorted({e["ts_code"] for e in long_entries})
    panels = _build_ohlc_panels(codes, long_entries, lookforward_days)
    price_df = panels["close"]
    if price_df.empty:
        return {"equity_curve": [], "stats": {}, "trades": []}

    entries_df, exits_df, trades_meta = _build_signals(
        long_entries, price_df.index, lookforward_days)

    if not entries_df.any().any():
        return {"equity_curve": [], "stats": {}, "trades": []}

    try:
        pf = vbt.Portfolio.from_signals(
            close=price_df,
            open=panels["open"], high=panels["high"], low=panels["low"],
            price=panels["open"],   # entry/exit 均 @ open（组合模式简化：在开盘成交）
            entries=entries_df, exits=exits_df,
            init_cash=1_000_000,
            size=0.20,
            size_type="Percent",   # 每笔投入组合权益的 20%（~5 并发上限，资金不足买得起多少买多少）
            cash_sharing=True,
            group_by=True,
            freq="D",
            fees=commission,          # 双边佣金；印花税(卖出)暂略(vbt 无单边费率), 略低估成本
            slippage=0.0,
        )
    except Exception as exc:
        print(f"⚠ run_portfolio vectorbt 失败: {type(exc).__name__}: {exc}")
        return {"equity_curve": [], "stats": {}, "trades": [], "error": str(exc)}

    equity = pf.value()
    equity_curve = [
        {"date": d.strftime("%Y-%m-%d"), "equity": round(float(v), 2)}
        for d, v in equity.items() if not np.isnan(v)
    ]

    stats = {
        "total_return": round(float(pf.total_return()), 4),
        "max_drawdown": round(float(pf.max_drawdown()), 4),
        "sharpe": round(float(pf.sharpe_ratio()), 4),
        "n_trades": int(pf.trades.count()),
        "win_rate": round(float(pf.trades.win_rate()), 4),
        "init_cash": 1_000_000,
        "final_equity": round(float(equity.iloc[-1]), 2) if len(equity) else None,
    }

    # 每笔成交明细（从 vbt trades 提取实际收益）
    trades = []
    try:
        trec = pf.trades.records_readable
        for _, t in trec.iterrows():
            trades.append({
                "ts_code": str(t.get("Column", "")),
                "entry_date": str(t.get("Entry Timestamp", ""))[:10],
                "exit_date": str(t.get("Exit Timestamp", ""))[:10],
                "return": round(float(t.get("Return", 0) or 0), 4),
            })
    except Exception:
        # trades 明细提取失败不致命，stats 已有
        pass

    return {"equity_curve": equity_curve, "stats": stats, "trades": trades}


# ── 打印 ──────────────────────────────────────────────────────────────────────

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
