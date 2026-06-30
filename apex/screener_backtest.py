"""Screener 报告回测 —— 按 strategy 回测历史 screener 候选，反哺策略权重。

v2（工程评审 2026-06-29）：扩展为因子评测层，回答"哪几个策略是废的"。
  - 两趟 run()：pass1 收集 row-level panel → pass2 聚合（胜率 + IC + pool-alpha + 4 态 verdict）
  - _simulate(return_gross=True) 返回 {1,5,10}d 多 horizon gross/net；return_gross=False 字节兼容旧胜率路径
  - within-pool Spearman IC（pandas df.corr(method=spearman)，不引 scipy）
  - pool-alpha vs 匹配基准（涨停池均值 / HS300；industry_rotation 基准不可历史重跑 → alpha_unavailable）
  - 4 态 verdict 优先级：n_insufficient > alpha_unavailable > dead_weight > live_candidate
  - 落盘 screener_strategy_stats.json（胜率，向后兼容 selector）+ factor_ic.json（IC/alpha/verdict）
  - CLI: python -m apex.screener_backtest 打 dead_weight 表

与 backtest.py（基于 journal AI verdict）的区别：
  - backtest.py 回测的是 AI verdict 信号，其 source 字段是 standalone/debate（analyze 上下文），
    无法映射到 screener 策略名。
  - 本模块直接回测 screener 每日报告里的候选股，按其归属策略（all_candidates_summary.strategy）
    聚合 → 直接映射 STRATEGIES，喂给 strategy_selector 做权重反哺。

报告格式：新版 all_candidates_summary=[{ts_code, strategy, strategy_score, weighted_score}]；
旧版（all_candidates + rule_score，无 strategy）跳过（无法映射策略）。
对每只候选: 信号日 D → T+1 开盘入场 → 持 N 天 → time_stop 出场（screener 不给 SL/TP）。
涨停封板 → unfillable（剔出胜率/IC 分母）。
"""
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import backtest as bt
from apex import config

_TZ_CN = timezone(timedelta(hours=8))

# IC / pool-alpha 评测 horizon（天）。胜率路径用 lookforward_days（默认 10，与 10d 对齐）。
_HORIZONS = (1, 5, 10)
_DEFAULT_LOOKFORWARD = 10

# 4 态 verdict 阈值（来自设计文档）
_MIN_FILLABLE_N = 10        # fillable_n < 此值 → n_insufficient
_MIN_N_DATES = 8            # 跨日期 IC 观测数 < 此值 → n_insufficient（外音 #2）
_MIN_BENCHMARK_N = 5        # 有基准的 fillable 行 < 此值 → alpha_unavailable
_IC_DEAD = 0.05             # |ic_5d| < 此值 视作无排序力
_ALPHA_DEAD = 0.01          # |pool_alpha_5d| < 此值 视作无超额

# per-strategy 匹配基准路由（设计 Premise 2 表）
# 涨停池均值：first_board_leader / leader_with_volume / volume_breakout（事件源自当日涨停池）
_LIMIT_UP_POOL_STRATS = {"first_board_leader", "leader_with_volume", "volume_breakout"}
# 强势行业板块均值：industry_rotation —— industry.fetch 取 akshare 当前快照、不可历史重跑 → alpha_unavailable
_INDUSTRY_STRATS = {"industry_rotation"}
# 小盘策略：stealth_accumulation / pullback_to_ma / bullish_alignment —— 选股天然偏小盘，
# 用 HS300（大盘）做基准会把小盘 beta 错记成 alpha（牛市小盘跑赢 HS300 ≠ 选股能力）。
# 改用中证 1000（000852.SH）做风格匹配基准，剥离小盘 beta。
_SMALL_CAP_STRATS = {"stealth_accumulation", "pullback_to_ma", "bullish_alignment"}
# institutional_flow 跟机构票（偏大盘）→ 仍用 HS300
_HS300_CODE = "000300.SH"
_CSI1000_CODE = "000852.SH"

# CLI 开关：跳过涨停池基准（首次冷启动池股 ~100-200 只未缓存 → 大量 tushare daily 调用 + 1/hour 限速）。
# 跳过时 limit_up_pool 策略 benchmark_n=0 → alpha_unavailable；IC/胜率/verdict 仍全算。
_SKIP_LIMIT_UP_BENCHMARK = False


# ── 路径 ───────────────────────────────────────────────────────────────────────

def _screener_dir() -> Path:
    return Path(config.get()["paths"]["screener_dir"]).expanduser()


def _cache_dir() -> Path:
    cfg = config.get()
    cache_dir = Path((cfg.get("paths") or {}).get("cache_dir", "~/.stock-journal/cache")).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _stats_path() -> Path:
    return _cache_dir() / "screener_strategy_stats.json"


def _factor_ic_path() -> Path:
    return _cache_dir() / "factor_ic.json"


def _limit_up_pool_cache_path(trade_date: str) -> Path:
    return _cache_dir() / f"limit_up_pool_{trade_date}.json"


# ── forward return 引擎 ─────────────────────────────────────────────────────────

def _simulate(code: str, signal_date: str, holding_period: int,
              return_gross: bool = False) -> Optional[dict]:
    """信号日 signal_date(YYYY-MM-DD) → T+1 open 入场 → 持 N 天 time_stop。

    return_gross=False（默认，字节兼容旧胜率路径）:
        返回 {net_return, hit, unfillable, exit_reason, fill_date, exit_date} 或 None（数据不足）。
    return_gross=True（IC/pool-alpha 路径）:
        返回 {unfillable, fill_date, fill_price, gross:{1,5,10}, net:{1,5,10}, exit_dates} 或 None。
        多 horizon：T+1 open 锚定，h 天后 close 出场；该 horizon 数据不足则该项 None。
        gross = (exit_close - fill_price)/fill_price（剥 _apply_costs，供成本对称的 pool-alpha）。

    复用 market_cache（已缓存，0 API）+ backtest 的成本/涨停逻辑。
    """
    from apex import market_cache
    start = signal_date.replace("-", "")
    end = (datetime.strptime(signal_date, "%Y-%m-%d") + timedelta(days=holding_period + 20)).strftime("%Y%m%d")
    df = market_cache.load_daily_full(code, start, end, adj="qfq")
    if df is None or df.empty or len(df) < 3:
        return None
    window = df.iloc[:holding_period + 2]
    if len(window) < 3:
        return None

    prev_close = float(window["close"].iloc[0])
    t1_open = float(window["open"].iloc[1])
    t1_high = float(window["high"].iloc[1])
    t1_low = float(window["low"].iloc[1])
    t1_close = float(window["close"].iloc[1])

    limit = bt._limit_pct(code)
    limit_up = prev_close * (1 + limit)
    unfillable = (t1_high == t1_low == t1_close) or \
                 (t1_open >= limit_up - 1e-4 and t1_high >= limit_up - 1e-4)
    if unfillable:
        if return_gross:
            return {
                "unfillable": True,
                "fill_date": None,
                "fill_price": None,
                "gross": {h: None for h in _HORIZONS},
                "net": {h: None for h in _HORIZONS},
                "exit_dates": {h: None for h in _HORIZONS},
            }
        return {"net_return": None, "hit": None, "unfillable": True, "exit_reason": "limit_unfillable"}

    fill_price = t1_open
    fill_date = window.index[1].strftime("%Y-%m-%d")

    if return_gross:
        gross: dict = {}
        net: dict = {}
        exit_dates: dict = {}
        for h in _HORIZONS:
            idx = 1 + h  # T+1 fill 在 window[1]，h 天后出场 window[1+h]
            if idx >= len(window):
                gross[h] = None
                net[h] = None
                exit_dates[h] = None
                continue
            exit_close = float(window["close"].iloc[idx])
            g = (exit_close - fill_price) / fill_price
            gross[h] = round(g, 6)
            net[h] = round(bt._apply_costs(g), 6)
            exit_dates[h] = window.index[idx].strftime("%Y-%m-%d")
        return {
            "unfillable": False,
            "fill_date": fill_date,
            "fill_price": round(fill_price, 4),
            "gross": gross,
            "net": net,
            "exit_dates": exit_dates,
        }

    # 旧胜率路径（字节兼容）
    exit_close = float(window["close"].iloc[-1])
    gross = (exit_close - fill_price) / fill_price
    net = bt._apply_costs(gross)
    return {
        "net_return": round(net, 4),
        "hit": net > 0,
        "unfillable": False,
        "exit_reason": "time_stop",
        "fill_date": fill_date,
        "exit_date": window.index[-1].strftime("%Y-%m-%d"),
    }


# ── 匹配基准 ───────────────────────────────────────────────────────────────────

def _load_limit_up_pool(trade_date: str) -> Optional[list[str]]:
    """当日涨停池 ts_code 列表（带落盘缓存，避开 limit_list_d 1次/小时限速，外音 #3）。
    缓存命中 → 0 API；未命中 → limit_up.fetch（可能限速失败）→ 落盘。
    fetch 失败/空池 → 返回 None（调用方标 benchmark_missing）。"""
    cache = _limit_up_pool_cache_path(trade_date)
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    try:
        from apex.signals import limit_up
        records = limit_up.fetch(trade_date.replace("-", ""))
    except Exception:
        records = []
    if not records:
        return None
    codes = [r["ts_code"] for r in records if r.get("ts_code")]
    if not codes:
        return None
    try:
        cache.write_text(json.dumps(codes, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return codes


def _limit_up_pool_benchmark_5d(trade_date: str, lookforward_days: int) -> Optional[float]:
    """当日涨停池均值 5d gross forward return。
    对池内每只股票走 _simulate(return_gross=True) 的同一 T+1-open 锚定 + unfillable 剔除，
    保证与因子侧成本对称、口径一致（池内封板买不进的剔出，正如因子侧）。
    无 fillable 池股 → None（benchmark_missing）。"""
    pool = _load_limit_up_pool(trade_date)
    if not pool:
        return None
    rets = []
    for code in pool:
        sim = _simulate(code, trade_date, lookforward_days, return_gross=True)
        if not sim or sim.get("unfillable"):
            continue
        g5 = (sim.get("gross") or {}).get(5)
        if g5 is not None:
            rets.append(g5)
    if not rets:
        return None
    return round(sum(rets) / len(rets), 6)


def _index_forward_return(code: str, signal_date: str, horizon: int) -> Optional[float]:
    """指数 close-to-close gross forward return（HS300 兜底基准，market_cache 0 API）。
    口径：signal_date D 为信号日，iloc[1]=T+1 close，iloc[1+h]=h 日后 close。
    与因子侧 T+1-open 锚定略有差异（指数只有 close），v1 接受。"""
    from apex import market_cache
    start = signal_date.replace("-", "")
    end = (datetime.strptime(signal_date, "%Y-%m-%d") + timedelta(days=horizon + 20)).strftime("%Y%m%d")
    s = market_cache.load_index_daily(code, start, end)
    if s is None or len(s) < horizon + 2:
        return None
    try:
        c0 = float(s.iloc[1])
        c1 = float(s.iloc[1 + horizon])
    except Exception:
        return None
    if c0 <= 0:
        return None
    return round(c1 / c0 - 1, 6)


def _build_benchmark(strategy: str, rows: list[dict], lookforward_days: int,
                     limit_up_cache: dict, hs300_cache: dict,
                     csi1000_cache: dict) -> tuple[str, dict]:
    """返回 (label, {signal_date: gross_5d or None})。同日涨停池/指数基准跨策略共用缓存。

    路由优先级：industry(不可重跑) > 涨停池 > 小盘(CSI1000) > 大盘(HS300)。
    小盘策略用 CSI1000 剥离小盘 beta，避免把 beta 错记成 pool_alpha。
    """
    dates = sorted({r["signal_date"] for r in rows})
    if strategy in _INDUSTRY_STRATS:
        # industry.fetch 取 akshare 当前快照、忽略 trade_date，不可历史重跑 → 永远 None
        return "industry_sector_unavailable", {d: None for d in dates}
    out: dict = {}
    if strategy in _LIMIT_UP_POOL_STRATS:
        for d in dates:
            if d not in limit_up_cache:
                if _SKIP_LIMIT_UP_BENCHMARK:
                    limit_up_cache[d] = None
                else:
                    limit_up_cache[d] = _limit_up_pool_benchmark_5d(d, lookforward_days)
            out[d] = limit_up_cache[d]
        return "limit_up_pool" if not _SKIP_LIMIT_UP_BENCHMARK else "limit_up_pool_skipped", out
    # 小盘策略 → 中证 1000；其余（institutional_flow 等）→ HS300
    if strategy in _SMALL_CAP_STRATS:
        for d in dates:
            if d not in csi1000_cache:
                csi1000_cache[d] = _index_forward_return(_CSI1000_CODE, d, 5)
            out[d] = csi1000_cache[d]
        return "csi1000", out
    for d in dates:
        if d not in hs300_cache:
            hs300_cache[d] = _index_forward_return(_HS300_CODE, d, 5)
        out[d] = hs300_cache[d]
    return "hs300", out


# ── IC / pool-alpha / verdict（纯函数，可单测）─────────────────────────────────

def _spearman_ic(scores: list[float], returns: list[float]) -> Optional[float]:
    """within-pool Spearman rank correlation。ties 用 average rank（pandas 默认）。
    样本<3 / score unique<3 / return 无方差 → None（不进聚合分母）。"""
    if len(scores) < 3 or len(scores) != len(returns):
        return None
    import pandas as pd
    df = pd.DataFrame({"s": scores, "r": returns})
    if df["s"].nunique() < 3 or df["r"].nunique() < 2:
        return None
    val = df.corr(method="spearman").loc["s", "r"]
    if pd.isna(val):
        return None
    return round(float(val), 4)


def _aggregate_ic(fillable_rows: list[dict], horizon: int) -> dict:
    """按 signal_date 分组，每组满足守卫（≥5 fillable & ≥3 unique score）才算一个 IC 观测。
    返回 {ic_mean, ic_std, n_dates, ir, t_stat}；n_dates=0 时前几项 None。"""
    by_date: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in fillable_rows:
        g = (r.get("sim", {}).get("gross") or {}).get(horizon)
        if g is None:
            continue
        by_date[r["signal_date"]].append((r["strategy_score"], g))

    ic_vals: list[float] = []
    for pairs in by_date.values():
        if len(pairs) < 5:
            continue
        scores = [p[0] for p in pairs]
        if len(set(scores)) < 3:
            continue
        ic = _spearman_ic(scores, [p[1] for p in pairs])
        if ic is not None:
            ic_vals.append(ic)

    n_dates = len(ic_vals)
    if n_dates == 0:
        return {"ic_mean": None, "ic_std": None, "n_dates": 0, "ir": None, "t_stat": None}
    mean = sum(ic_vals) / n_dates
    std = math.sqrt(sum((v - mean) ** 2 for v in ic_vals) / n_dates)  # 总体标准差
    ir = round(mean / std, 4) if std > 0 else None
    t_stat = round(mean / std * math.sqrt(n_dates), 4) if std > 0 else None
    return {"ic_mean": round(mean, 4), "ic_std": round(std, 4), "n_dates": n_dates, "ir": ir, "t_stat": t_stat}


def _pool_alpha(fillable_rows: list[dict], benchmark_by_date: dict) -> dict:
    """pool-alpha @5d = 该策略 fillable 行 gross[5] − 当日基准 gross_5d，取均值。
    成本对称：两侧都 gross（因子侧剥 _apply_costs，基准侧 gross）。基准缺失的行计 missing 不计分母。
    返回 {pool_alpha, benchmark_n, benchmark_missing_count}。"""
    alphas: list[float] = []
    bn = 0
    missing = 0
    for r in fillable_rows:
        g5 = (r.get("sim", {}).get("gross") or {}).get(5)
        if g5 is None:
            continue
        b = benchmark_by_date.get(r["signal_date"])
        if b is None:
            missing += 1
            continue
        bn += 1
        alphas.append(g5 - b)
    if not alphas:
        return {"pool_alpha": None, "benchmark_n": bn, "benchmark_missing_count": missing}
    return {
        "pool_alpha": round(sum(alphas) / len(alphas), 6),
        "benchmark_n": bn,
        "benchmark_missing_count": missing,
    }


def _verdict(fillable_n: int, n_dates_5d: int, benchmark_n: int,
             ic_5d: Optional[float], pool_alpha_5d: Optional[float]) -> str:
    """4 态 verdict 优先级：n_insufficient > alpha_unavailable > dead_weight > live_candidate。
    - n_insufficient: fillable_n<10 或 n_dates_5d<8（外音 #2 守卫）
    - alpha_unavailable: benchmark_n<5（含 industry_rotation 基准不可历史重跑）
    - dead_weight: |ic_5d|<0.05 AND |pool_alpha_5d|<0.01（两项都弱，n 已够）
    - live_candidate: 其余
    industry_rotation 因基准恒 None → benchmark_n 恒 0 → 够 n 后落 alpha_unavailable（绝不默认 live_candidate）。"""
    if fillable_n < _MIN_FILLABLE_N or n_dates_5d < _MIN_N_DATES:
        return "n_insufficient"
    if benchmark_n < _MIN_BENCHMARK_N:
        return "alpha_unavailable"
    if ic_5d is not None and pool_alpha_5d is not None \
            and abs(ic_5d) < _IC_DEAD and abs(pool_alpha_5d) < _ALPHA_DEAD:
        return "dead_weight"
    return "live_candidate"


# ── 主入口 ─────────────────────────────────────────────────────────────────────

def run(lookforward_days: int = _DEFAULT_LOOKFORWARD) -> dict:
    """扫描全部 screener 报告，两趟聚合：胜率（screener_strategy_stats.json）+
    IC/pool-alpha/verdict（factor_ic.json）。返回 factor_ic payload。"""
    reports = sorted(_screener_dir().glob("*.jsonl"))

    # ── pass 1: 收集 row-level panel ──
    panel: list[dict] = []
    total = 0
    reports_used = 0
    for rp in reports:
        try:
            d = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        cands = d.get("all_candidates_summary") or []
        if not cands:
            continue  # 旧版报告（all_candidates + rule_score，无 strategy）跳过
        reports_used += 1
        trade_date = d.get("trade_date") or rp.stem
        for c in cands:
            code = c.get("ts_code")
            strat = c.get("strategy")
            if not code or not strat:
                continue
            total += 1
            sim = _simulate(code, trade_date, lookforward_days, return_gross=True)
            if sim is None:
                continue
            try:
                score = float(c.get("strategy_score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            panel.append({
                "ts_code": code,
                "strategy": strat,
                "strategy_score": score,
                "signal_date": trade_date,
                "sim": sim,
            })

    # ── pass 2: per-strategy 聚合 ──
    by_strat_rows: dict[str, list[dict]] = defaultdict(list)
    for r in panel:
        by_strat_rows[r["strategy"]].append(r)

    limit_up_bench_cache: dict[str, Optional[float]] = {}
    hs300_bench_cache: dict[str, Optional[float]] = {}
    csi1000_bench_cache: dict[str, Optional[float]] = {}

    by_strategy_stats: list[dict] = []
    by_strategy_ic: list[dict] = []

    for strat in sorted(by_strat_rows):
        rows = by_strat_rows[strat]
        n = len(rows)
        fillable = [r for r in rows if not r["sim"].get("unfillable")]
        unfillable_count = n - len(fillable)
        fillable_n = len(fillable)

        # 旧胜率（用 lookforward horizon 的 net；默认 10d）
        net_returns = [(r["sim"].get("net") or {}).get(lookforward_days) for r in fillable
                       if lookforward_days in _HORIZONS]
        if lookforward_days not in _HORIZONS:
            net_returns = [(r["sim"].get("net") or {}).get(_DEFAULT_LOOKFORWARD) for r in fillable]
        net_returns = [x for x in net_returns if x is not None]
        win = sum(1 for x in net_returns if x > 0)
        by_strategy_stats.append({
            "key": strat,
            "n": n,
            "fillable_n": fillable_n,
            "unfillable_count": unfillable_count,
            "win_rate": round(win / len(net_returns), 4) if net_returns else None,
            "avg_net_return": round(sum(net_returns) / len(net_returns), 4) if net_returns else None,
        })

        # 匹配基准（按 date；同日涨停池/指数跨策略共用）
        benchmark_label, benchmark_by_date = _build_benchmark(
            strat, rows, lookforward_days, limit_up_bench_cache, hs300_bench_cache,
            csi1000_bench_cache)

        # IC per horizon
        ic = {h: _aggregate_ic(fillable, h) for h in _HORIZONS}

        # pool-alpha @5d
        pa = _pool_alpha(fillable, benchmark_by_date)

        n_dates_5d = ic[5]["n_dates"]
        verdict = _verdict(fillable_n, n_dates_5d, pa["benchmark_n"],
                           ic[5]["ic_mean"], pa["pool_alpha"])

        by_strategy_ic.append({
            "key": strat,
            "n": n,
            "fillable_n": fillable_n,
            "unfillable_count": unfillable_count,
            "benchmark": benchmark_label,
            "benchmark_n": pa["benchmark_n"],
            "benchmark_missing_count": pa["benchmark_missing_count"],
            "ic_1d": ic[1]["ic_mean"],
            "ic_5d": ic[5]["ic_mean"],
            "ic_10d": ic[10]["ic_mean"],
            "ir_5d": ic[5]["ir"],
            "t_stat_5d": ic[5]["t_stat"],
            "n_dates_5d": n_dates_5d,
            "pool_alpha_5d": pa["pool_alpha"],
            "verdict": verdict,
        })

    by_strategy_stats.sort(key=lambda x: -(x["fillable_n"]))
    by_strategy_ic.sort(key=lambda x: -(abs(x["ic_5d"]) if x["ic_5d"] is not None else 0))

    now = datetime.now(_TZ_CN).isoformat(timespec="seconds")
    stats_payload = {
        "generated_at": now,
        "lookforward_days": lookforward_days,
        "total_candidates": total,
        "reports_scanned": len(reports),
        "reports_used": reports_used,
        "by_strategy": by_strategy_stats,
    }
    try:
        _stats_path().write_text(json.dumps(stats_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠ screener_strategy_stats 落盘失败: {e}")

    ic_payload = {
        "generated_at": now,
        "lookforward_days": lookforward_days,
        "horizons": list(_HORIZONS),
        "reports_scanned": len(reports),
        "reports_used": reports_used,
        "total_candidates": total,
        "thresholds": {
            "min_fillable_n": _MIN_FILLABLE_N,
            "min_n_dates": _MIN_N_DATES,
            "min_benchmark_n": _MIN_BENCHMARK_N,
            "ic_dead": _IC_DEAD,
            "alpha_dead": _ALPHA_DEAD,
        },
        "by_strategy": by_strategy_ic,
    }
    try:
        _factor_ic_path().write_text(json.dumps(ic_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠ factor_ic 落盘失败: {e}")

    return ic_payload


def load_strategy_stats() -> Optional[dict]:
    """供 strategy_selector 读取已落盘的 screener 策略模拟胜率。无则 None。"""
    p = _stats_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_factor_ic() -> Optional[dict]:
    """供 selector（T8 接通后）读取 IC/pool-alpha/verdict。无则 None。"""
    p = _factor_ic_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ── CLI ────────────────────────────────────────────────────────────────────────

def _fmt(x, width: int, spec: str) -> str:
    if x is None:
        return "—".rjust(width)
    return format(x, spec).rjust(width)


def _print_table(payload: dict) -> None:
    by = payload.get("by_strategy") or []
    n_reports = payload.get("reports_used", 0)
    print()
    print(f"⚠ 样本 n={n_reports} 份报告，结论当先验不当定论（N≥30 date-cells/策略 再严肃采信）")
    print(f"  verdict 优先级: n_insufficient > alpha_unavailable > dead_weight > live_candidate")
    print()
    header = (f"{'strategy':<22}{'n':>4}{'fill':>5}{'IC5d':>8}{'IR':>7}"
              f"{'t':>7}{'alpha5d':>9}{'bnchN':>7}{'ndates':>7}  verdict")
    print(header)
    print("-" * len(header))
    for r in by:
        line = (
            f"{r['key']:<22}"
            f"{r['n']:>4}"
            f"{r['fillable_n']:>5}"
            f"{_fmt(r['ic_5d'], 8, '.3f')}"
            f"{_fmt(r['ir_5d'], 7, '.2f')}"
            f"{_fmt(r['t_stat_5d'], 7, '.2f')}"
            f"{_fmt(r['pool_alpha_5d'], 9, '.4f')}"
            f"{r['benchmark_n']:>7}"
            f"{r['n_dates_5d']:>7}"
            f"  {r['verdict']}"
        )
        print(line)
    print()
    # loud 汇总
    counts = defaultdict(int)
    for r in by:
        counts[r["verdict"]] += 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"汇总: {summary}")
    print()


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Screener 因子评测：IC + pool-alpha + 4 态 verdict")
    ap.add_argument("--lookforward", type=int, default=_DEFAULT_LOOKFORWARD,
                    help=f"胜率路径 holding 天数（默认 {_DEFAULT_LOOKFORWARD}，IC 固定 {{1,5,10}}d）")
    ap.add_argument("--no-benchmark", action="store_true",
                    help="跳过涨停池基准（首次冷启动池股未缓存会触发大量 tushare 调用 + 1/hour 限速）；"
                         "跳过时 limit_up_pool 策略 → alpha_unavailable，IC/胜率/verdict 仍全算")
    args = ap.parse_args()
    global _SKIP_LIMIT_UP_BENCHMARK
    _SKIP_LIMIT_UP_BENCHMARK = args.no_benchmark
    payload = run(lookforward_days=args.lookforward)
    _print_table(payload)


if __name__ == "__main__":
    main()
