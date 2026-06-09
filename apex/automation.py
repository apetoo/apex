"""apex 全自动交易助手 — 每天自动盯盘、分析、筛选。

用法:
  python -m apex.automation          # 跑一次完整全天流程（按当前时段执行对应任务）
  python -m apex.automation --loop   # 长连模式：交易时段持续运行，到点自动干活
  python -m apex.automation --status # 查看今天干了什么

长连模式自动按时间表执行：
  09:00  晨报 + 检查持仓/候选触发
  10:00  分析候选股 #1
  13:00  分析候选股 #2
  14:30  分析候选股 #3
  15:30  盘后筛选器
  全天    连续盯触发价（复用 monitor）
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
        result = screener.run(ai_enabled=True)
        count = len(result.get("results", []))
        print(f"\n  → 筛选完成: {count} 条结果")
        state = _load_state()
        state["last_screener"] = _today()
        _save_state(state)
        return f"screener_{count}"
    except Exception as e:
        print(f"\n  ✗ 筛选失败: {e}")
        return "screener_fail"


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
    elif "--status" in sys.argv:
        show_status()
    else:
        run_once()
