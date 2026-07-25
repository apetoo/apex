"""apex 健康检查 — 供 CLI 调用，输出结构化状态报告。

用法:
  python -m apex.health                      # 完整检查
  python -m apex.health --checks automation,watchlist  # 指定检查项
  python -m apex.health --json               # JSON 输出（适合自动化消费）

返回码：0 = 正常，1 = 有警告，2 = 有错误
"""

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_TZ_CN = timezone(timedelta(hours=8))
_REPO_ROOT = Path(__file__).resolve().parent.parent


# ── 检查项 ────────────────────────────────────────────────────────


def check_automation() -> dict:
    """检查自动化运行状态：今天各时段任务是否已完成。"""
    state_path = Path.home() / ".stock-journal" / "automation_state.json"
    if not state_path.exists():
        return {
            "status": "error",
            "label": "自动化状态",
            "summary": "状态文件不存在 — 自动化从未运行过？",
            "detail": "",
        }

    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return {
            "status": "error",
            "label": "自动化状态",
            "summary": f"状态文件读取失败: {e}",
            "detail": "",
        }

    today_str = datetime.now(_TZ_CN).strftime("%Y-%m-%d")
    is_weekday = datetime.now(_TZ_CN).weekday() < 5

    if state.get("date") != today_str:
        if is_weekday:
            return {
                "status": "warning",
                "label": "自动化状态",
                "summary": f"今日 ({today_str}) 尚未运行 — 上次运行: {state.get('date', '从未')}",
                "detail": f"已完成任务: {state.get('tasks_done', [])}",
            }
        else:
            return {
                "status": "ok",
                "label": "自动化状态",
                "summary": "非交易日，无需运行",
                "detail": f"上次运行: {state.get('date', '从未')}",
            }

    tasks_done = state.get("tasks_done", [])
    analyzed = state.get("analyzed_today", [])
    detail_parts = [f"已完成任务 ({len(tasks_done)}): {', '.join(tasks_done)}"]
    if analyzed:
        detail_parts.append(f"今日分析: {', '.join(analyzed)}")
    if state.get("last_screener"):
        detail_parts.append(f"最近筛选: {state['last_screener']}")

    # 检查是否所有时段任务都已跑完（盘后）
    hm = datetime.now(_TZ_CN).strftime("%H:%M")
    expected = ["task_briefing"]
    if hm >= "10:00":
        expected.extend(["task_analyze_one"])
    if hm >= "13:00":
        expected.extend(["task_analyze_one"])  # 第二只
    if hm >= "14:30":
        expected.extend(["task_analyze_one"])  # 第三只
    if hm >= "15:30":
        expected.append("task_screener")

    done_set = set(tasks_done)
    missed = [t for t in expected if t not in done_set]

    if missed:
        return {
            "status": "warning",
            "label": "自动化状态",
            "summary": f"今日已运行，但有 {len(missed)} 个待完成任务",
            "detail": " | ".join(detail_parts) + f" | 待完成: {', '.join(missed)}",
        }

    return {
        "status": "ok",
        "label": "自动化状态",
        "summary": f"今日所有时段任务已完成 ✓",
        "detail": " | ".join(detail_parts),
    }


def check_watchlist() -> dict:
    """检查 watchlist 健康度：过期条目、候选触发、止损/目标接近。"""
    from apex import watchlist, data

    wl = watchlist.load()
    issues = []

    # 检查过期条目
    expired = watchlist.expire_stale(wl)
    if expired:
        issues.append(f"{len(expired)} 个过期条目已归档: {', '.join(expired[:5])}")

    active = wl.get("active_positions", [])
    candidates = wl.get("candidates", [])
    archived = wl.get("archived", [])

    n_pos = len(active)
    n_cand = len(candidates)
    n_arch = len(archived)

    # 获取实时价格检查触发
    codes = [p["ts_code"] for p in active] + [c["ts_code"] for c in candidates]
    codes = [c for c in codes if c]
    prices = {}
    if codes:
        prices = data.get_realtime_price(codes)
        if not any(v is not None for v in prices.values()):
            prices = data.get_latest_price(codes)

    triggered_pos, triggered_cand = watchlist.get_triggered(prices)

    if triggered_pos:
        detail_lines = []
        for p in triggered_pos:
            t = p.get("trigger_price", "?")
            cur = p.get("current_price", "?")
            detail_lines.append(f"  🛑 {p.get('name','')} ({p['ts_code']}) 现价={cur} 触发位={t}")
        issues.append(f"{len(triggered_pos)} 个持仓触发！" + "\n" + "\n".join(detail_lines))

    if triggered_cand:
        detail_lines = []
        for c in triggered_cand:
            t = c.get("trigger_price", "?")
            cur = c.get("current_price", "?")
            detail_lines.append(f"  ⚡ {c.get('name','')} ({c['ts_code']}) 现价={cur} 触发价={t}")
        issues.append(f"{len(triggered_cand)} 个候选触发！" + "\n" + "\n".join(detail_lines))

    summary = f"持仓 {n_pos} | 候选 {n_cand} | 归档 {n_arch}"
    if issues:
        return {
            "status": "warning" if not triggered_pos else "error",
            "label": "Watchlist",
            "summary": summary,
            "detail": "\n".join(issues),
        }

    return {
        "status": "ok",
        "label": "Watchlist",
        "summary": summary,
        "detail": "无异常",
    }


def check_git() -> dict:
    """检查 git 仓库状态：未提交变更、未推送提交。"""
    try:
        # 未暂存/未提交的变更
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=_REPO_ROOT, timeout=10,
        )
        changes = [line for line in result.stdout.strip().split("\n") if line.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {
            "status": "error",
            "label": "Git 状态",
            "summary": f"检查失败: {e}",
            "detail": "",
        }

    if not changes:
        return {
            "status": "ok",
            "label": "Git 状态",
            "summary": "工作区干净",
            "detail": "",
        }

    modified = [c for c in changes if c.startswith(" M") or c.startswith("M")]
    untracked = [c for c in changes if c.startswith("??")]
    staged = [c for c in changes if c.startswith("A") or c.startswith("M ")]

    detail_lines = []
    if untracked:
        detail_lines.append(f"  未跟踪 ({len(untracked)}): {', '.join(c[3:] for c in untracked[:5])}")
    if modified:
        detail_lines.append(f"  已修改 ({len(modified)}): {', '.join(c[3:] for c in modified[:5])}")
    if staged:
        detail_lines.append(f"  已暂存 ({len(staged)})")

    # 检查未推送提交
    try:
        unpushed = subprocess.run(
            ["git", "log", "--oneline", "@{u}..HEAD"],
            capture_output=True, text=True, cwd=_REPO_ROOT, timeout=10,
        )
        unpushed_commits = [l for l in unpushed.stdout.strip().split("\n") if l.strip()]
        if unpushed_commits:
            detail_lines.append(f"  未推送 ({len(unpushed_commits)} 个提交)")
    except Exception:
        pass

    return {
        "status": "warning" if changes else "ok",
        "label": "Git 状态",
        "summary": f"{len(changes)} 个未提交变更" + (f" ({len(unpushed_commits)} 未推送)" if unpushed_commits else ""),
        "detail": "\n".join(detail_lines) if detail_lines else "",
    }


def check_journal(days: int = 3) -> dict:
    """检查近期 journal：bullish/bearish 信号分布。"""
    from apex import journal

    all_entries = journal.load_verdicts()
    cutoff = (datetime.now(_TZ_CN) - timedelta(days=days)).isoformat()

    recent = [e for e in all_entries if (e.get("analyzed_at") or e.get("date", "")) >= cutoff]

    if not recent:
        return {
            "status": "ok",
            "label": f"近 {days} 日分析",
            "summary": "无分析记录",
            "detail": "",
        }

    # 按 ts_code 分组
    by_code: dict[str, list[dict]] = {}
    for e in recent:
        code = e.get("ts_code", "unknown")
        by_code.setdefault(code, []).append(e)

    bullish = {"看多", "偏多", "观望偏多"}
    bearish = {"看空", "偏空", "观望偏空"}
    counts = {"bullish": 0, "bearish": 0, "neutral": 0, "total": len(recent)}

    recent_verdicts = []
    for e in recent:
        v = e.get("verdict", "?")
        if v in bullish:
            counts["bullish"] += 1
        elif v in bearish:
            counts["bearish"] += 1
        else:
            counts["neutral"] += 1
        recent_verdicts.append(f"{e.get('ts_code','?')}: {v}")

    detail_lines = []
    for code, entries in sorted(by_code.items()):
        # 找最后一条有 verdict 的 entry，避免显示 None
        latest = None
        for e in reversed(entries):
            if e.get("verdict"):
                latest = e
                break
        if not latest:
            latest = entries[-1]
        v = latest.get("verdict", "?")
        c = latest.get("confidence", "?")
        detail_lines.append(f"  {code}: {v} (置信度 {c})")

    summary = f"{counts['total']} 条分析 — 看多 {counts['bullish']} / 中性 {counts['neutral']} / 看空 {counts['bearish']}"

    if counts["bullish"] > counts["bearish"] and counts["bullish"] >= 2:
        status = "ok"
    elif counts["bearish"] > counts["bullish"] and counts["bearish"] >= 2:
        status = "warning"
    else:
        status = "ok"

    return {
        "status": status,
        "label": f"近 {days} 日分析",
        "summary": summary,
        "detail": "\n".join(detail_lines),
    }


def check_process() -> dict:
    """检查 apex.automation --loop 是否在运行。"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "apex.automation.*--loop"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # macOS 没有 pgrep？试试 ps
        try:
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True, text=True, timeout=10,
            )
            pids = []
            for line in result.stdout.split("\n"):
                if "apex.automation" in line and "--loop" in line and "grep" not in line:
                    parts = line.split()
                    if parts:
                        pids.append(parts[1])
        except Exception as e:
            return {
                "status": "error",
                "label": "自动化进程",
                "summary": f"检查失败: {e}",
                "detail": "",
            }

    if pids:
        return {
            "status": "ok",
            "label": "自动化进程",
            "summary": f"运行中 (PID {', '.join(pids)})",
            "detail": "",
        }

    return {
        "status": "warning",
        "label": "自动化进程",
        "summary": "未运行 — apex.automation --loop 未启动",
        "detail": "使用 `cd apex && .venv/bin/python -m apex.automation --loop` 启动",
    }


def check_closed_positions(days: int = 7) -> dict:
    """检查近期已平仓记录。"""
    from apex import watchlist

    closed = watchlist.load_closed_positions(since_days=days)
    if not closed:
        return {
            "status": "ok",
            "label": f"近 {days} 日平仓",
            "summary": "无平仓记录",
            "detail": "",
        }

    total_pnl_pct = 0
    wins = 0
    losses = 0
    detail_lines = []
    win_rate_total = 0

    for rec in closed:
        close_info = rec.get("close", {})
        pnl_pct = close_info.get("realized_pnl_pct")
        pnl_amt = close_info.get("realized_pnl_amount")
        days_held = close_info.get("days_held", "?")
        exit_reason = close_info.get("exit_reason", "?")
        name = rec.get("name", rec.get("ts_code", "?"))

        if pnl_pct is not None:
            total_pnl_pct += pnl_pct
            if pnl_pct > 0:
                wins += 1
            else:
                losses += 1

        pnl_str = f"{pnl_pct*100:.1f}%" if pnl_pct is not None else "?"
        amt_str = f"¥{pnl_amt:.1f}" if pnl_amt is not None else ""
        detail_lines.append(
            f"  {name}: {pnl_str} {amt_str} | {days_held}天 | {exit_reason}"
        )

    total = wins + losses
    win_rate = f"{wins}/{total} ({wins*100//total}%)" if total > 0 else "N/A"
    pnl_total_str = f"{total_pnl_pct*100:.1f}%" if total > 0 else "0%"

    status = "ok"
    if total_pnl_pct < -0.1:
        status = "warning"
    elif total_pnl_pct < -0.2:
        status = "error"

    return {
        "status": status,
        "label": f"近 {days} 日平仓",
        "summary": f"{len(closed)} 笔 | 胜率 {win_rate} | 累计 {pnl_total_str}",
        "detail": "\n".join(detail_lines),
    }


# ── 报告生成 ──────────────────────────────────────────────────────


_CHECKS = {
    "automation": check_automation,
    "watchlist": check_watchlist,
    "git": check_git,
    "journal": check_journal,
    "process": check_process,
    "closed": check_closed_positions,
}


def run(checks: Optional[list[str]] = None, json_output: bool = False) -> list[dict]:
    """运行指定（或全部）检查项，返回结果列表。"""
    if checks:
        names = [c.strip() for c in checks if c.strip() in _CHECKS]
    else:
        names = list(_CHECKS.keys())

    results = []
    for name in names:
        try:
            fn = _CHECKS[name]
            r = fn()
        except Exception as e:
            r = {
                "status": "error",
                "label": name,
                "summary": f"检查异常: {type(e).__name__}: {e}",
                "detail": "",
            }
        r["name"] = name
        results.append(r)

    return results


def print_report(results: list[dict]) -> int:
    """输出可读报告。返回最高严重级别 (0=ok, 1=warning, 2=error)。"""
    max_severity = 0
    severity_map = {"ok": 0, "warning": 1, "error": 2}

    lines = []
    lines.append("")
    lines.append(f"{'='*55}")
    lines.append(f"  📊 apex 健康检查 — {datetime.now(_TZ_CN).strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"{'='*55}")
    lines.append("")

    for r in results:
        sev = severity_map.get(r["status"], 0)
        max_severity = max(max_severity, sev)

        icon = {"ok": "✅", "warning": "⚠️", "error": "🚨"}.get(r["status"], "❓")
        lines.append(f"  {icon} {r['label']}")
        lines.append(f"     {r['summary']}")
        if r.get("detail"):
            for d_line in r["detail"].split("\n"):
                lines.append(f"     {d_line}")
        lines.append("")

    print("\n".join(lines))
    return max_severity


# ── 入口 ──────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="apex 健康检查")
    parser.add_argument("--checks", help="检查项，逗号分隔 (默认全部)")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    check_list = args.checks.split(",") if args.checks else None
    results = run(checks=check_list, json_output=args.json)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    return print_report(results)


if __name__ == "__main__":
    sys.exit(main())
