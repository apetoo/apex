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


# ── chat 扩展工具的 digest（逐笔流水 / 账户风控 / 仓位计算 / 平仓诊断）─────────────


def trade_history_digest(trades: list[dict]) -> str:
    """逐笔买卖流水摘要。

    与 ``closed_trades_digest`` 互补：后者看闭环交易（判决→盈亏→退出原因），
    这里看 ``trades.jsonl`` 的原始 buy/sell 流水（加仓/减仓/部分平仓/单笔节奏）。
    每条一行：``时间 | 标的 | 方向 | 成交价 | 股数 | 金额 | 持仓变化 | 策略 | 备注``。
    末尾汇总 buy/sell 笔数。
    """
    if not trades:
        return "无交易流水记录。"

    buys = sum(1 for t in trades if t.get("side") == "buy")
    sells = sum(1 for t in trades if t.get("side") == "sell")
    lines = [f"## 最近 {len(trades)} 笔交易流水（买入 {buys} / 卖出 {sells}）"]
    lines.append("时间 | 标的 | 方向 | 成交价 | 股数 | 金额 | 持仓变化 | 策略 | 备注")
    lines.append("---|---|---|---|---|---|---|---|---")

    for t in trades:
        ts = (t.get("traded_at") or "?")[:16].replace("T", " ")
        code = t.get("ts_code", "?")
        name = _short(t.get("name"))
        side = "买" if t.get("side") == "buy" else "卖"
        price = t.get("fill_price", "?")
        shares = t.get("shares", "?")
        amount = t.get("amount")
        amt_str = f"{amount:,.0f}" if isinstance(amount, (int, float)) else "?"
        after = t.get("shares_after")
        after_str = f"{after}" if after is not None else "?"
        strat = t.get("strategy") or "-"
        note = t.get("note") or ""
        lines.append(
            f"{ts} | {code} {name} | {side} | {price} | {shares} | {amt_str} | "
            f"持{after_str} | {strat} | {note}"
        )

    return "\n".join(lines)


def account_risk_digest(risk: dict, account: dict) -> str:
    """账户风控摘要：总资金 / 单笔风险 / 总风险上限 + 当前持仓风险敞口是否超限。

    ``risk`` 来自 ``account.current_total_risk(active_positions)``，``account`` 来自
    ``account.load()``。用户问『仓位重不重/总风险多少/超限没』时调。
    """
    capital = account.get("total_capital", 0)
    rpt = account.get("risk_per_trade_pct", 0)
    max_pct = account.get("max_total_risk_pct", 0)
    total_risk = risk.get("total_risk_amount", 0)
    risk_pct = risk.get("total_risk_pct")
    over = risk.get("over_limit")
    pos_n = risk.get("position_count", 0)
    missing = risk.get("missing_size_count", 0)

    pct_str = f"{risk_pct:.2f}%" if isinstance(risk_pct, (int, float)) else "?"
    status = "⚠️ 超出上限" if over else "未超限"
    missing_str = f"（另有 {missing} 条缺 size 数据未计入）" if missing else ""

    lines = [
        "## 账户风控",
        f"总资金: ¥{capital:,.0f} | 单笔风险: {rpt}% | 总风险上限: {max_pct}%",
        f"当前持仓风险敞口: ¥{total_risk:,.0f} ({pct_str}) — {status}",
        f"计入持仓: {pos_n} 条{missing_str}",
    ]
    return "\n".join(lines)


def position_size_digest(ps: dict) -> str:
    """仓位计算结果摘要。``ps`` 来自 ``account.compute_position_size(entry, stop)``。"""
    if not ps.get("ok"):
        reasons = "；".join(ps.get("warnings") or []) or "无法计算"
        return f"## 仓位计算\n❌ 无法给出建议：{reasons}"

    shares = ps["shares"]
    lots = shares // 100
    risk_amt = ps["risk_amount"]
    capital_req = ps["capital_required"]
    risk_used = ps["risk_pct_used"]
    risk_actual = ps["risk_pct_actual"]

    lines = [
        "## 仓位计算结果",
        f"→ 建议买入 {shares} 股（{lots} 手）",
        f"承担风险: ¥{risk_amt:,.0f}（{risk_actual:.3f}% 实际 / {risk_used}% 预算）",
        f"所需资金: ¥{capital_req:,.0f}",
    ]
    for w in ps.get("warnings") or []:
        lines.append(f"⚠️ {w}")
    return "\n".join(lines)


def trade_diagnosis_digest(record: dict) -> str:
    """单笔已平仓交易的 AI 事后诊断摘要。

    从 closed_position record 的 ``diagnosis`` 字段取（平仓时 ``postmortem.run_and_patch``
    写回）。``closed_trades_digest`` 故意丢弃 diagnosis 全文（怕多条爆 context），
    这里按需取单笔展开：判断对/漏/教训/全文。
    """
    diag = record.get("diagnosis") or {}
    if not diag:
        return "该笔交易暂无 AI 事后诊断（平仓时未生成或失败）。"

    o = record.get("open") or {}
    c = record.get("close") or {}
    ts = record.get("ts_code", "?")
    name = _short(record.get("name"))
    verdict = o.get("ai_verdict") or "-"
    conf = o.get("ai_confidence")
    conf_str = f"{conf}" if conf is not None else "?"
    pnl = c.get("realized_pnl_pct")
    days = c.get("trading_days_held")
    reason = c.get("exit_reason") or "-"
    days_str = f"{days}日" if days is not None else "-"

    lines = [f"## 平仓诊断 — {ts} {name}"]
    lines.append(
        f"AI判决: {verdict}({conf_str}) | 实际盈亏: {_pct(pnl)} | "
        f"持仓 {days_str} | 退出: {reason}"
    )

    correctly = diag.get("ai_correctly_identified") or []
    missed = diag.get("ai_missed") or []
    lesson = diag.get("lesson") or ""
    text = diag.get("ai_diagnosis_text") or ""

    if correctly:
        lines.append(f"\n**判断对的**：{'；'.join(map(str, correctly))}")
    if missed:
        lines.append(f"\n**判断漏的/错的**：{'；'.join(map(str, missed))}")
    if lesson:
        lines.append(f"\n**教训**：{lesson}")
    if text:
        lines.append(f"\n**诊断全文**：\n{text}")

    return "\n".join(lines)
