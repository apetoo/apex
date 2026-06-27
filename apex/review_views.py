"""chat 复盘专用派生视图 —— 把原始用户行为数据压成 chat 工具可读的紧凑摘要。

存储层保持全量原始（closed_positions.jsonl / watchlist.json / calibration.json /
evidence_attribution.json）；这里只做"读出来后压成什么"的派生，**不写入、不落盘、
不调 compute()**（compute 在平仓时已跑过，热路径再跑会有延迟，见 calibration.load 注释）。

设计动机：chat 定位是"用户投资助理 + 复盘"，不是"又一个分析股票的 AI"（那是
analyze 的活）。这些视图让 chat 能回答"我哪些判断错了 / 哪个策略在赚钱 / 哪类
证据预测准"——analyze（事前判断）做不到的差异化。

仿 ``apex/journal_views.py`` 的模式：读原始 dict → 压成紧凑字符串，下游 chat
工具直接塞回 tool message。
"""
from __future__ import annotations

from collections import Counter
from typing import Optional


def _pct(x: Optional[float]) -> str:
    """小数 → '+1.23%' / '-0.50%' / '-'。realized_pnl_pct 是小数（0.0123=+1.23%）。"""
    if x is None:
        return "-"
    try:
        return f"{float(x) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "-"


def _short(s, n: int = 8) -> str:
    """股票名截断，避免长名撑爆行宽。"""
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def closed_trades_digest(records: list[dict], limit: int = 20) -> str:
    """已平仓交易复盘摘要。

    每条一行：``日期 | ts_code 名 | AI判决(置信度) | 策略 | 持仓天数 | 实际盈亏 | 退出原因``
    末尾汇总：总笔数 / 胜率 / 平均盈亏 / 最大赚 / 最大亏。

    **丢弃** open.ai_analysis_text 全文 / ai_features / diagnosis 详情（按需另开工具），
    否则多条会爆 context。
    """
    if not records:
        return "无已平仓交易记录。"

    shown = records[:limit]
    lines = [f"## 最近 {len(shown)} 笔已平仓交易（共 {len(records)} 笔）"]
    lines.append("日期 | 标的 | AI判决(置信度) | 策略 | 持仓天数 | 实际盈亏 | 退出原因")
    lines.append("---|---|---|---|---|---|---")

    pnls: list[float] = []
    wins = losses = 0
    for r in shown:
        o = r.get("open") or {}
        c = r.get("close") or {}
        date = str(c.get("exit_date") or (c.get("closed_at") or "")[:10])
        ts = r.get("ts_code", "?")
        name = _short(r.get("name"))
        verdict = o.get("ai_verdict") or "-"
        conf = o.get("ai_confidence")
        conf_str = f"{conf}" if conf is not None else "?"
        strategy = o.get("strategy") or "-"
        days = c.get("trading_days_held")
        days_str = f"{days}日" if days is not None else "-"
        pnl = c.get("realized_pnl_pct")
        reason = c.get("exit_reason") or "-"

        if isinstance(pnl, (int, float)):
            pnls.append(float(pnl))
            if pnl > 0.005:
                wins += 1
            elif pnl < -0.005:
                losses += 1

        lines.append(
            f"{date} | {ts} {name} | {verdict}({conf_str}) | {strategy} | "
            f"{days_str} | {_pct(pnl)} | {reason}"
        )

    if pnls:
        n = len(pnls)
        win_rate = wins / n
        avg = sum(pnls) / n
        best = max(pnls)
        worst = min(pnls)
        lines.append("")
        lines.append(
            f"**汇总**：{n} 笔 | 胜率 {win_rate * 100:.0f}% ({wins}胜{losses}负) | "
            f"平均 {avg * 100:+.2f}% | 最大赚 {_pct(best)} | 最大亏 {_pct(worst)}"
        )

    return "\n".join(lines)


def watchlist_digest(wl: Optional[dict]) -> str:
    """当前持仓/候选/归档摘要。让模型知道用户手里有什么。"""
    if not wl:
        return "无 watchlist 数据。"

    actives = wl.get("active_positions") or []
    candidates = wl.get("candidates") or []
    archived = wl.get("archived") or []

    lines = [f"## 当前盘面（{len(actives)} 持仓 / {len(candidates)} 候选 / {len(archived)} 归档）"]

    if actives:
        lines.append("\n### 持仓")
        lines.append("标的 | 入场价 | 止损 | 目标 | 策略 | 置信度")
        lines.append("---|---|---|---|---|---")
        for p in actives:
            ts = p.get("ts_code", "?")
            name = _short(p.get("name"))
            entry = p.get("entry_price")
            stop = p.get("stop_loss")
            target = p.get("target")
            strat = p.get("strategy") or "-"
            conf = p.get("calibrated_confidence")
            conf_str = f"{conf:.0f}" if isinstance(conf, (int, float)) else "-"
            lines.append(
                f"{ts} {name} | {entry} | {stop} | {target} | {strat} | {conf_str}"
            )
    else:
        lines.append("\n### 持仓\n（空）")

    if candidates:
        lines.append("\n### 候选（待触发）")
        lines.append("标的 | 触发价 | 策略 | 到期")
        lines.append("---|---|---|---")
        for c in candidates:
            ts = c.get("ts_code", "?")
            name = _short(c.get("name"))
            trig = c.get("trigger_price")
            strat = c.get("strategy") or "-"
            exp = c.get("expires_at") or "-"
            lines.append(f"{ts} {name} | {trig} | {strat} | {exp}")
    else:
        lines.append("\n### 候选\n（空）")

    if archived:
        reasons = Counter(
            (a.get("status") or "archived").replace("archived_", "").replace("archived", "manual")
            for a in archived
        )
        reason_str = " / ".join(f"{r}×{n}" for r, n in reasons.most_common())
        lines.append(f"\n### 归档（{len(archived)} 条）：{reason_str}")

    return "\n".join(lines)


def calibration_digest(cal: Optional[dict]) -> str:
    """历史胜率校准摘要。复用 calibration.format_for_prompt 生成 markdown 表。"""
    if not cal:
        return "暂无平仓校准数据（平仓后会自动积累）。"

    eligible = int(cal.get("eligible_for_calibration") or 0)
    total = int(cal.get("total_closed") or 0)
    header = (
        f"## 历史胜率校准（{total} 笔平仓，{eligible} 笔可用于校准）\n"
        f"computed_at: {cal.get('computed_at', '?')}"
    )

    # 复用现成的 markdown 表生成器（n_min=3 同 analyze 注入门槛）
    from apex.calibration import format_for_prompt
    table = format_for_prompt(n_min=3, calibration=cal)
    if not table:
        return header + "\n\n样本不足（每桶需 ≥3 笔），暂无可用胜率表。"
    return header + "\n\n" + table


def evidence_digest(ea: Optional[dict]) -> str:
    """证据归因摘要：哪类证据预测准/是噪音。复用 evidence_attribution.format_for_prompt。"""
    if not ea:
        return "暂无证据归因数据（平仓后会自动积累）。"

    total_ev = int(ea.get("total_evidence") or 0)
    matched = int(ea.get("matched_evidence") or 0)
    with_outcome = int(ea.get("entries_with_outcome") or 0)
    header = (
        f"## 证据归因（{total_ev} 条 evidence，{matched} 条命中分类，"
        f"{with_outcome} 条关联到已结仓结果）"
    )

    from apex.evidence_attribution import format_for_prompt
    table = format_for_prompt(n_min=3, outcome_n_min=5, attribution=ea)
    if not table:
        return header + "\n\n样本不足（每 pattern 需 ≥3 次引用），暂无可用归因表。"
    return header + "\n\n" + table
