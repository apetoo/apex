"""Screener 报告回测 —— 按 strategy 回测历史 screener 候选，反哺策略权重。

与 backtest.py（基于 journal AI verdict）的区别：
  - backtest.py 回测的是 AI verdict 信号，其 source 字段是 standalone/debate（analyze 上下文），
    无法映射到 screener 策略名。
  - 本模块直接回测 screener 每日报告里的候选股，按其归属策略（all_candidates_summary.strategy）
    聚合胜率 → 直接映射 STRATEGIES，喂给 strategy_selector 做权重反哺。

报告结构（新版）: all_candidates_summary = [{ts_code, strategy, strategy_score, ...}]
对每只候选: 信号日 D → T+1 开盘入场 → 持 N 天 → time_stop 出场（screener 不给 SL/TP）。
涨停封板 → unfillable（剔出胜率分母）。
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import backtest as bt
from apex import config

_TZ_CN = timezone(timedelta(hours=8))


def _screener_dir() -> Path:
    return Path(config.get()["paths"]["screener_dir"]).expanduser()


def _stats_path() -> Path:
    cfg = config.get()
    cache_dir = Path((cfg.get("paths") or {}).get("cache_dir", "~/.stock-journal/cache")).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "screener_strategy_stats.json"


def _simulate(code: str, signal_date: str, holding_period: int) -> Optional[dict]:
    """信号日 signal_date(YYYY-MM-DD) → T+1 open 入场 → 持 N 天 time_stop。

    返回 {net_return, hit, unfillable, exit_reason, fill_date, exit_date} 或 None（数据不足）。
    复用 market_cache（已缓存，N 只候选 0 次 API）+ backtest 的成本/涨停/基准逻辑。
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
        return {"net_return": None, "hit": None, "unfillable": True, "exit_reason": "limit_unfillable"}

    fill_price = t1_open
    exit_close = float(window["close"].iloc[-1])
    gross = (exit_close - fill_price) / fill_price
    net = bt._apply_costs(gross)
    return {
        "net_return": round(net, 4),
        "hit": net > 0,
        "unfillable": False,
        "exit_reason": "time_stop",
        "fill_date": window.index[1].strftime("%Y-%m-%d"),
        "exit_date": window.index[-1].strftime("%Y-%m-%d"),
    }


def run(lookforward_days: int = 10) -> dict:
    """扫描全部 screener 报告，按策略聚合模拟胜率，落盘供 strategy_selector 读取。"""
    reports = sorted(_screener_dir().glob("*.jsonl"))
    agg: dict = {}
    total = 0
    for rp in reports:
        try:
            d = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        trade_date = d.get("trade_date") or rp.stem  # 报告存的是 YYYY-MM-DD
        # 兼容新旧报告：新版 all_candidates_summary 带 strategy；旧版无则跳过
        cands = d.get("all_candidates_summary") or []
        for c in cands:
            code = c.get("ts_code")
            strat = c.get("strategy")
            if not code or not strat:
                continue
            total += 1
            res = _simulate(code, trade_date, lookforward_days)
            if res is None:
                continue
            a = agg.setdefault(strat, {"n": 0, "fillable_n": 0, "unfillable": 0, "win": 0, "net_sum": 0.0})
            a["n"] += 1
            if res["unfillable"]:
                a["unfillable"] += 1
                continue
            a["fillable_n"] += 1
            a["net_sum"] += res["net_return"]
            if res["hit"]:
                a["win"] += 1

    by_strategy = []
    for strat, a in agg.items():
        fn = a["fillable_n"]
        by_strategy.append({
            "key": strat,
            "n": a["n"],
            "fillable_n": fn,
            "unfillable_count": a["unfillable"],
            "win_rate": round(a["win"] / fn, 4) if fn else None,
            "avg_net_return": round(a["net_sum"] / fn, 4) if fn else None,
        })
    by_strategy.sort(key=lambda x: -(x["fillable_n"]))

    payload = {
        "generated_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
        "lookforward_days": lookforward_days,
        "total_candidates": total,
        "reports_scanned": len(reports),
        "by_strategy": by_strategy,
    }
    try:
        _stats_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠ screener_strategy_stats 落盘失败: {e}")
    return payload


def load_strategy_stats() -> Optional[dict]:
    """供 strategy_selector 读取已落盘的 screener 策略模拟胜率。无则 None。"""
    p = _stats_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
