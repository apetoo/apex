"""apex 全自动交易助手 — 每天自动盯盘、分析、筛选。

用法:
  python -m apex.automation          # 跑一次完整全天流程（按当前时段执行对应任务）
  python -m apex.automation --loop   # 长连模式：交易时段持续运行，到点自动干活
  python -m apex.automation --auto-trade  # 盘后一次性自动撮合 + 粗筛入候选（服务器 cron 用）
  python -m apex.automation --status # 查看今天干了什么

长连模式自动按时间表执行：
  09:00  晨报 + 检查持仓/候选触发
  10:00  分析候选股 #1
  13:00  分析候选股 #2
  14:30  分析候选股 #3
  15:30  盘后筛选器
  全天    连续盯触发价（复用 monitor）

--auto-trade 模式（模拟实盘，需 config.auto_trade.enabled=true）：
  1. 自动撮合（OHLC 回放）：先平仓触发止损/止盈/到期的持仓，再 promote 触发候选
  2. 盘后粗筛 + 分析 top N + bullish 自动入候选（供次日撮合）
  成交价=trigger_price，T+1，单只失败不中断，幂等靠状态机。
  建议 tushare 当日数据更新后（~18:30）由 cron 调用（loop 模式 15:30 进 postmarket
  当日数据未出会跑空，故撮合走 cron 不走 loop）。
"""

import json
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_TZ_CN = timezone(timedelta(hours=8))

# ── 状态管理 ──────────────────────────────────────────────────────

def _state_path() -> Path:
    return Path.home() / ".stock-journal" / "automation_state.json"


def _load_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"date": "", "tasks_done": [], "analyzed_today": [], "last_screener": ""}


def _save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _today() -> str:
    return datetime.now(_TZ_CN).strftime("%Y-%m-%d")


def _is_trading_day() -> bool:
    return datetime.now(_TZ_CN).weekday() < 5


def _is_trading_hours() -> bool:
    now = datetime.now(_TZ_CN)
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    return ("09:25" <= hm <= "11:30") or ("12:55" <= hm <= "15:05")


def _current_period() -> str:
    """返回当前时段标签。"""
    hm = datetime.now(_TZ_CN).strftime("%H:%M")
    if hm < "09:00":
        return "premarket"
    elif hm < "09:30":
        return "morning_ready"
    elif hm < "11:30":
        return "morning"
    elif hm < "13:00":
        return "lunch"
    elif hm < "15:00":
        return "afternoon"
    elif hm < "15:30":
        return "close"
    else:
        return "postmarket"


# ── 代理清理 ──────────────────────────────────────────────────────

def _clean_proxy():
    import os
    for k in list(os.environ):
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(k, None)


# ── 任务定义 ──────────────────────────────────────────────────────

def task_briefing() -> str:
    """晨报 + watchlist 检查"""
    _clean_proxy()
    from apex import briefing, watchlist, data
    print("\n" + "=" * 50)
    print("  📋 晨报 & Watchlist 检查")
    print("=" * 50)
    briefing.run()
    # 检查候选触发
    wl = watchlist.load()
    candidates = wl.get("candidates", [])
    if candidates:
        codes = [c["ts_code"] for c in candidates if c.get("ts_code")]
        prices = data.get_realtime_price(codes)
        if not any(v is not None for v in prices.values()):
            prices = data.get_latest_price(codes)
        triggered = []
        for c in candidates:
            code = c.get("ts_code")
            price = prices.get(code)
            trigger = c.get("trigger_price")
            if price and trigger and watchlist.is_triggered(c, price):
                triggered.append(f"  ⚡ {code} {c.get('name','')} 触发价={trigger} 现价={price:.2f}")
        if triggered:
            print("\n🟢 候选触发提醒：")
            for t in triggered:
                print(t)
        else:
            print("\n⏳ 候选均未触发")
    return "briefing"


def task_analyze_one() -> str:
    """从候选或归档中选一只股票分析"""
    _clean_proxy()
    from apex import watchlist, analyze, journal
    state = _load_state()

    # 今天已经分析过的，跳过
    analyzed_today = set(state.get("analyzed_today", []))

    # 优先分析候选股
    wl = watchlist.load()
    candidates = wl.get("candidates", [])
    to_analyze = None
    for c in candidates:
        code = c.get("ts_code")
        if code and code not in analyzed_today:
            to_analyze = code
            break

    # 候选都分析完了，从归档里找最近没分析的
    if not to_analyze:
        archived = wl.get("archived", [])
        # 找最近归档的、且不是 archived_promoted（已成交的）
        for a in reversed(archived):
            code = a.get("ts_code")
            status = a.get("status", "")
            if code and code not in analyzed_today and "promoted" not in status:
                to_analyze = code
                break

    if not to_analyze:
        print("\n⏭ 没有待分析的股票")
        return "analyze_skip"

    print(f"\n{'='*50}")
    print(f"  🔍 分析 {to_analyze}")
    print("=" * 50)
    try:
        result = analyze.run(to_analyze, save=True)
        verdict = result.get("verdict", "?")
        conf = result.get("confidence", "?")
        print(f"\n  → {verdict} (置信度 {conf})")
        # 更新状态
        state.setdefault("analyzed_today", []).append(to_analyze)
        _save_state(state)
        return f"analyze_{to_analyze}_{verdict}"
    except Exception as e:
        print(f"\n  ✗ 分析失败: {e}")
        return f"analyze_fail_{to_analyze}"


def task_screener() -> str:
    """盘后筛选器"""
    _clean_proxy()
    from apex import screener
    print("\n" + "=" * 50)
    print("  🔎 盘后筛选器")
    print("=" * 50)
    try:
        result = screener.run()
        count = len(result.get("results", []))
        print(f"\n  → 筛选完成: {count} 条结果")
        state = _load_state()
        state["last_screener"] = _today()
        _save_state(state)
        return f"screener_{count}"
    except Exception as e:
        print(f"\n  ✗ 筛选失败: {e}")
        return "screener_fail"


# ── 自动撮合（模拟实盘，--auto-trade 用）─────────────────────────

def _auto_trade_cfg() -> dict:
    """读 config.auto_trade，带默认值兜底（config 未配也能跑）。"""
    from apex import config
    cfg = (config.get().get("auto_trade") or {})
    return {
        "enabled": cfg.get("enabled", False),
        "top_n_picks": cfg.get("top_n_picks", 5),
        "holding_period_days": cfg.get("holding_period_days", 10),
        "bullish_verdicts": cfg.get("bullish_verdicts", ["看多", "偏多"]),
    }


def _fetch_day_ohlc(ts_code: str, day: str) -> Optional[dict]:
    """拉某股某日 OHLC（真实价 adj=none）。day 为 'YYYY-MM-DD'。失败/无数据返 None。"""
    from apex import market_cache
    d = day.replace("-", "")
    try:
        df = market_cache.load_daily_full(ts_code, start_date=d, end_date=d, adj="none")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    try:
        row = df.iloc[0]
        return {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
    except Exception:
        return None


def _triggered_today(candidate: dict, ohlc: dict) -> bool:
    """用今日 OHLC 判定候选是否在日内触发（复用 is_triggered 的带状/单向语义）。"""
    if not ohlc:
        return False
    low = ohlc["low"]; high = ohlc["high"]
    tl = candidate.get("trigger_low"); th = candidate.get("trigger_high")
    if tl is not None and th is not None:
        return float(low) <= float(th) and float(high) >= float(tl)
    trigger = candidate.get("trigger_price")
    if trigger is None:
        return False
    direction = candidate.get("trigger_direction", "below")
    if direction == "below":
        return low <= float(trigger)
    if direction == "above":
        return high >= float(trigger)
    return False


def task_auto_trade() -> str:
    """盘后 OHLC 回放撮合：先 close 旧持仓（止损/止盈/到期），再 promote 触发候选。

    补断层 C+D。成交价=trigger_price；T+1（entry_date<今日才判 close）；
    单只失败不中断；幂等靠状态机（archive/移出后不重复成交）。
    """
    _clean_proxy()
    acfg = _auto_trade_cfg()
    if not acfg["enabled"]:
        print("\n  ⏭ auto_trade 未启用，跳过自动撮合")
        return "autotrade_skip_disabled"
    from apex import watchlist
    print("\n" + "=" * 50)
    print("  🤖 自动撮合（OHLC 回放）")
    print("=" * 50)

    today = _today()
    holding_period = acfg["holding_period_days"]
    closed_n = 0
    promoted_n = 0

    # 1. 先 close：entry_date < 今日 的持仓，用今日 OHLC 判止损/止盈/到期
    wl = watchlist.load()
    for pos in list(wl.get("active_positions", [])):
        code = pos.get("ts_code")
        entry_date = pos.get("entry_date") or ""
        if not code or entry_date >= today:
            continue  # T+1：今日新 promote 的不判 close
        ohlc = _fetch_day_ohlc(code, today)
        if not ohlc:
            continue  # 今日数据未出，跳过该只
        stop = pos.get("stop_loss"); target = pos.get("target")
        exit_price = None; exit_reason = None
        # 同日同时触及止损止盈 -> 保守按先止损（最坏情况）
        if stop is not None and ohlc["low"] <= float(stop):
            exit_price, exit_reason = float(stop), "stop_hit"
        elif target is not None and ohlc["high"] >= float(target):
            exit_price, exit_reason = float(target), "target_hit"
        else:
            try:
                days_held = (datetime.strptime(today, "%Y-%m-%d") -
                             datetime.strptime(entry_date, "%Y-%m-%d")).days
            except Exception:
                days_held = 0
            if days_held >= holding_period:
                exit_price, exit_reason = ohlc["close"], "expired"
        if exit_price is None:
            continue
        try:
            watchlist.close_position(
                ts_code=code, exit_price=exit_price,
                exit_reason=exit_reason, exit_date=today,
                user_notes=f"auto_trade ({exit_reason})",
            )
            closed_n += 1
            print(f"  🔴 平仓 {code} @{exit_price:.2f} ({exit_reason})")
        except Exception as e:
            print(f"  ✗ 平仓 {code} 失败: {e}")

    # 2. 再 promote：候选用今日 OHLC 判触发
    wl = watchlist.load()  # 重新加载（close 后状态变了）
    active_codes = {p.get("ts_code") for p in wl.get("active_positions", [])}
    for cand in list(wl.get("candidates", [])):
        code = cand.get("ts_code")
        if not code or code in active_codes:
            continue  # 已有持仓，不重复 promote
        if cand.get("expires_at") and cand["expires_at"] < today:
            continue  # 过期候选不撮合（信号失效）
        ohlc = _fetch_day_ohlc(code, today)
        if not ohlc:
            continue
        if not _triggered_today(cand, ohlc):
            continue
        trigger = cand.get("trigger_price")
        if trigger is None or float(trigger) <= 0:
            continue
        stop = float(cand.get("stop_advice") or float(trigger) * 0.93)   # 缺省 -7% 止损
        target = float(cand.get("target_advice") or float(trigger) * 1.10)  # 缺省 +10% 目标
        try:
            watchlist.promote_candidate(
                ts_code=code,
                entry_price=float(trigger),
                stop_loss=stop,
                target=target,
                strategy=cand.get("strategy"),
            )
            promoted_n += 1
            print(f"  🟢 成交 {code} @{float(trigger):.2f} (trigger)")
        except Exception as e:
            print(f"  ✗ 成交 {code} 失败: {e}")

    print(f"\n  -> 平仓 {closed_n} 笔，成交 {promoted_n} 笔")
    return f"autotrade_closed_{closed_n}_promoted_{promoted_n}"


def task_screener_and_promote() -> str:
    """盘后粗筛 -> 分析 top N -> bullish 自动入候选（补断层 A+B）。

    链路：screener.run 取 top_n_picks -> 逐个 analyze.run ->
    verdict ∈ bullish_verdicts 且不重复 -> add_candidate(trigger=price_advice.entry,
    stop_advice=price_advice.stop_loss, target_advice=price_advice.target)。
    缺 stop_loss/target 时用 entry*0.93/1.10 兜底（对齐复盘 -7% 止损口径）。
    """
    _clean_proxy()
    acfg = _auto_trade_cfg()
    if not acfg["enabled"]:
        print("\n  ⏭ auto_trade 未启用，跳过粗筛入候选")
        return "screener_skip_disabled"
    from apex import screener, analyze, watchlist, data
    print("\n" + "=" * 50)
    print("  🔎 盘后粗筛 + 分析入候选")
    print("=" * 50)

    # 1. 粗筛（今日已跑过则复用 jsonl，避免重复花 DeepSeek）
    existing = screener.load_by_date(_today())
    if existing and existing.get("top_scored"):
        picks = existing["top_scored"]
        print(f"\n  复用今日粗筛缓存: {len(picks)} 条")
    else:
        try:
            result = screener.run(on_progress=lambda m: print(f"    {m}"))
            picks = result.get("top_scored", []) or []
        except Exception as e:
            print(f"\n  ✗ 粗筛失败: {e}")
            return "screener_fail"
    print(f"\n  粗筛完成: {len(picks)} 条")

    # 2. 取 top N（按 ai_score 降序，缺省 score）
    def _score(p):
        s = p.get("ai_score")
        return s if s is not None else p.get("score", 0)
    picks = sorted(picks, key=_score, reverse=True)[: acfg["top_n_picks"]]

    # 3. 逐个分析 + bullish 入候选
    wl = watchlist.load()
    existing_codes = ({c.get("ts_code") for c in wl.get("candidates", [])} |
                      {p.get("ts_code") for p in wl.get("active_positions", [])})
    promoted = 0
    for pick in picks:
        code = pick.get("ts_code")
        if not code:
            continue
        code = data.normalize_ts_code(code)
        if code in existing_codes:
            print(f"  ⏭ {code} 已在候选/持仓，跳过")
            continue
        print(f"  🔍 分析 {code} {pick.get('name','')} ...")
        try:
            res = analyze.run(code, save=True)
        except Exception as e:
            print(f"  ✗ 分析 {code} 失败: {e}")
            continue
        verdict = res.get("verdict", "?")
        pa = res.get("price_advice") or {}
        entry = pa.get("entry")
        print(f"    -> {verdict} (entry={entry})")
        if verdict not in acfg["bullish_verdicts"]:
            continue
        if not entry or float(entry) <= 0:
            print(f"  ⏭ {code} bullish 但无有效 entry，跳过入候选")
            continue
        stop = float(pa.get("stop_loss") or float(entry) * 0.93)
        target = float(pa.get("target") or float(entry) * 1.10)
        try:
            watchlist.add_candidate(
                code, pick.get("name", ""),
                trigger_price=float(entry),
                trigger_direction="below",
                stop_advice=stop,
                target_advice=target,
                strategy=pick.get("strategy"),
            )
            existing_codes.add(code)
            promoted += 1
            print(f"  ✅ {code} 入候选 (trigger={entry}, stop={stop:.2f}, target={target:.2f})")
        except Exception as e:
            print(f"  ✗ {code} 入候选失败: {e}")

    state = _load_state()
    state["last_screener"] = _today()
    _save_state(state)
    return f"screener_{len(picks)}_promoted_{promoted}"


# ── 调度表 ────────────────────────────────────────────────────────

SCHEDULE = [
    # (触发时段标签, 执行函数, 描述)
    ("morning_ready", task_briefing, "晨报"),
    ("morning", task_analyze_one, "盘中分析 #1"),
    ("afternoon", task_analyze_one, "盘中分析 #2"),
    ("close", task_analyze_one, "尾盘分析 #3"),
    ("postmarket", task_screener, "盘后筛选"),
]


def _should_run(task_key: str) -> bool:
    """检查今天这个任务是否已经执行过"""
    state = _load_state()
    if state.get("date") != _today():
        return True  # 新的一天，全部重置
    done = set(state.get("tasks_done", []))
    return task_key not in done


def _mark_done(task_key: str) -> None:
    state = _load_state()
    if state.get("date") != _today():
        state = {"date": _today(), "tasks_done": [], "analyzed_today": []}
    done = set(state.get("tasks_done", []))
    done.add(task_key)
    state["tasks_done"] = list(done)
    _save_state(state)


# ── 单次运行（按当前时段执行对应任务）────────────────────────────

def run_once() -> list[str]:
    """执行当前时段应当运行的任务。返回已执行的任务 key 列表。"""
    if not _is_trading_day():
        print("📅 非交易日，跳过")
        return []

    period = _current_period()
    executed = []

    # 重置状态到新的一天
    state = _load_state()
    if state.get("date") != _today():
        state = {"date": _today(), "tasks_done": [], "analyzed_today": []}
        _save_state(state)
        print(f"\n📅 新交易日 {_today()}，状态已重置")

    print(f"\n⏰ 当前时段: {period}")

    for trigger_period, task_fn, desc in SCHEDULE:
        task_key = task_fn.__name__
        if trigger_period == period and _should_run(task_key):
            print(f"\n▶ 执行: {desc}")
            try:
                task_fn()
                _mark_done(task_key)
                executed.append(task_key)
                print(f"  ✓ {desc} 完成")
            except Exception as e:
                print(f"  ✗ {desc} 失败: {e}")

    if not executed:
        print("  当前时段无待执行任务")

    return executed


# ── 循环模式 ──────────────────────────────────────────────────────

def loop() -> None:
    """长连模式：交易时段持续运行，到点自动干活 + 持续盯触发。"""
    from apex.monitor import check_once

    print(f"\n{'='*50}")
    print(f"  🤖 apex 自动化助手启动")
    print(f"  {datetime.now(_TZ_CN).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")
    print("  全天时间表：")
    for _, _, desc in SCHEDULE:
        print(f"    • {desc}")
    print("    • 持续盯触发价")
    print(f"{'='*50}\n")

    last_period = ""

    while True:
        try:
            now = datetime.now(_TZ_CN)
            if not _is_trading_day():
                print(f"[{now.strftime('%H:%M')}] 非交易日，5分钟后重试...")
                time.sleep(300)
                continue

            period = _current_period()

            # 时段变化时执行对应任务
            if period != last_period:
                print(f"\n{'='*40}")
                print(f"  [{now.strftime('%H:%M')}] 时段: {period}")
                print(f"{'='*40}")
                run_once()
                last_period = period
                # 任务执行后睡 5 秒，避免重复触发
                time.sleep(5)

            # 交易时段每 60 秒跑一次 trigger 监控
            if _is_trading_hours():
                try:
                    res = check_once()
                    if res["triggers"]:
                        print(
                            f"  [{now.strftime('%H:%M')}] ⚡ {len(res['triggers'])} 个新触发 "
                            f"(已推送 {res['notified']} 条)"
                        )
                except Exception as e:
                    pass  # 监控失败不中断主循环

            time.sleep(60)

        except KeyboardInterrupt:
            print("\n\n🛑 自动化助手已停止")
            break
        except Exception as e:
            print(f"\n⚠ 循环异常: {type(e).__name__}: {e}")
            time.sleep(60)


# ── 状态查询 ──────────────────────────────────────────────────────

def run_auto_trade() -> list[str]:
    """一次性跑自动撮合 + 粗筛入候选（cron 友好）。

    建议在 tushare 当日数据更新后（~18:30）调用。
    顺序：先撮合（平仓昨日持仓 + promote 触发候选），再粗筛入候选（供次日撮合）。
    """
    print(f"\n{'='*50}")
    print(f"  🤖 auto-trade 一次性运行 - {_today()}")
    print(f"{'='*50}")
    executed = []
    for fn, desc in [(task_auto_trade, "自动撮合"), (task_screener_and_promote, "粗筛入候选")]:
        print(f"\n▶ {desc}")
        try:
            key = fn()
            executed.append(key)
            print(f"  ✓ {desc} 完成")
        except Exception as e:
            print(f"  ✗ {desc} 失败: {e}")
    return executed


def show_status() -> None:
    state = _load_state()
    print(f"\n{'='*40}")
    print(f"  📊 自动化状态")
    print(f"{'='*40}")
    if state.get("date") == _today():
        print(f"  日期: {state['date']} ✅ 今日已运行")
        done = state.get("tasks_done", [])
        print(f"  已完成任务 ({len(done)}):")
        for d in done:
            print(f"    • {d}")
        analyzed = state.get("analyzed_today", [])
        if analyzed:
            print(f"  今日分析: {', '.join(analyzed)}")
        if state.get("last_screener"):
            print(f"  最近筛选: {state['last_screener']}")
    else:
        print(f"  最后运行: {state.get('date', '从未')}")
        print("  今日尚未运行")


# ── 入口 ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--loop" in sys.argv:
        loop()
    elif "--auto-trade" in sys.argv:
        run_auto_trade()
    elif "--status" in sys.argv:
        show_status()
    else:
        run_once()
