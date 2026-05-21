"""
DeepSeek API (OpenAI-compatible) agent for stock analysis.
The AI autonomously calls data tools, then records verdict via record_verdict tool.
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_TZ_CN = timezone(timedelta(hours=8))

from openai import OpenAI

from apex import config, data, journal, calibration, evidence_attribution, trace as trace_mod
from apex.schemas import VERDICT_ENUM, BULLISH_VERDICTS, BEARISH_VERDICTS


class AnalysisError(Exception):
    pass


# ── Tool definitions (OpenAI function-calling format) ────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_daily_price",
            "description": "获取A股日K线数据（含MA5/MA10/MA20/MA60/量比）。返回最近60根K线的JSON。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码，如 002050.SZ"},
                    "start_date": {"type": "string", "description": "开始日期 YYYYMMDD，默认120天前"},
                    "end_date": {"type": "string", "description": "结束日期 YYYYMMDD，默认今天"},
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fundamentals",
            "description": "获取股票基本面数据：PE、PB、PS、换手率、流通市值。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码"},
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_info",
            "description": "获取公司名称、行业、上市日期。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string"},
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "用博查搜索股票相关信息。**必须分类搜索，不要混搜**。\n"
                "每次调用只查一个 category，AI 应分多次调用覆盖不同维度。\n"
                "\n"
                "**强制类别（record_verdict 前必须全部调用过，否则系统拒绝记录结论）**：\n"
                "  · earnings           — 业绩面（季报/预告/营收/净利润），oneMonth 窗口\n"
                "  · shareholders       — 股东动态（减持/增持/解禁/大宗交易），oneMonth 窗口\n"
                "  · regulatory         — 监管/合规（立案/处罚/诉讼/问询函），oneYear 窗口\n"
                "  · money_flow         — 资金面（北向/龙虎榜/主力/机构），oneWeek 窗口\n"
                "\n"
                "**可选类别（按需追加）**：\n"
                "  · corporate_actions  — 资本运作（定增/回购/重组/并购），oneYear 窗口\n"
                "  · research           — 卖方研报（评级/目标价变化），oneMonth 窗口\n"
                "  · industry           — 行业政策（需先用 get_stock_info 拿到 industry 后传入），oneYear 窗口\n"
                "  · general            — 兜底自定义 query\n"
                "\n"
                "返回结果已按信任度排序：cninfo/sse/szse > 东财/同花顺/雪球 > 其他自媒体。\n"
                "高信任来源（官方公告）的权重应明显高于自媒体。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码，如 002050.SZ"},
                    "category": {
                        "type": "string",
                        "enum": [
                            "earnings", "shareholders", "regulatory", "money_flow",
                            "corporate_actions", "research", "industry", "general",
                        ],
                        "description": "搜索类别（必填）。一次调用只能选一个。",
                    },
                    "name": {
                        "type": "string",
                        "description": "公司名。除 industry/general 外强烈建议传入，否则只能用代码召回，质量差。",
                    },
                    "industry": {
                        "type": "string",
                        "description": "行业名。仅当 category=industry 时使用（必填）。",
                    },
                    "query": {
                        "type": "string",
                        "description": "自定义搜索词。仅当 category=general 时生效。",
                    },
                    "freshness": {
                        "type": "string",
                        "enum": ["oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"],
                        "description": "可选；通常用 category 内置默认值，特殊场景才覆盖。",
                    },
                    "count": {"type": "integer", "description": "返回条数，默认 10"},
                },
                "required": ["ts_code", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dragon_tiger_list",
            "description": (
                "查个股近 N 天龙虎榜上榜情况（akshare 东财源，免费）。\n"
                "返回上榜日期清单 + 上榜频次。可选拉最近 3 次的买卖席位 TOP5（机构/游资）。\n"
                "\n"
                "**何时调用**：当 web_search(money_flow) 召回里出现「龙虎榜」字样、"
                "或股价短期异动需要确认是否游资炒作时调用。"
                "比 web_search 召回更结构化、可直接引用具体上榜次数和净买额。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码，如 002050.SZ"},
                    "days": {
                        "type": "integer",
                        "description": "回溯天数（自然日），默认 90",
                    },
                    "fetch_seats": {
                        "type": "boolean",
                        "description": "是否拉最近 3 次的买卖席位 TOP5（默认 false，需要时再开）",
                    },
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_unlock_schedule",
            "description": (
                "查个股限售解禁日程（akshare 东财源，免费）。\n"
                "返回未来 180 天待解禁明细 + 近 180 天已发生解禁（含解禁后 20 日表现）。\n"
                "\n"
                "**何时调用**：分析多头判断前必查。短期内大额解禁（占流通盘 > 5%）是"
                "重要利空信号。比 web_search(shareholders) 召回的「公告减持」更精确、可直接"
                "引用解禁日期 / 股份数 / 占流通比例。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码，如 002050.SZ"},
                    "days_ahead": {
                        "type": "integer",
                        "description": "未来回看天数，默认 180",
                    },
                    "history_days": {
                        "type": "integer",
                        "description": "历史回看天数（评估以往解禁后股价反应），默认 180",
                    },
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_verdict",
            "description": (
                "记录最终分析结论。分析完成后必须调用此工具，不得省略。\n"
                "**evidence 字段为必填**：每条格式「数据点 → 推论」，至少 3 条，"
                "必须引用工具返回的真实数字（如 close=12.34、RSI=67.2、减持公告日期），"
                "不得写泛泛的定性描述。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": VERDICT_ENUM,
                        "description": "判断方向",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "置信度 1-10",
                    },
                    "entry": {"type": "number", "description": "建议买入价（可选）"},
                    "stop_loss": {"type": "number", "description": "止损价（可选）"},
                    "target": {"type": "number", "description": "目标价（可选）"},
                    "features": {
                        "type": "object",
                        "description": "技术特征快照",
                        "properties": {
                            "ma5_position": {"type": "string", "enum": ["above", "below"]},
                            "ma20_position": {"type": "string", "enum": ["above", "below"]},
                            "ma_alignment": {"type": "string", "enum": ["bullish", "bearish", "mixed"]},
                            "volume_ratio": {"type": "number"},
                            "macd_zone": {"type": "string", "enum": ["above_zero", "below_zero"]},
                            "rsi_14": {"type": "number"},
                            "price_vs_ma5_pct": {"type": "number"},
                        },
                        "required": ["ma5_position", "ma20_position", "volume_ratio", "rsi_14"],
                    },
                    "evidence": {
                        "type": "array",
                        "description": (
                            "支撑结论的关键证据列表，格式：「数据点 → 推论」。"
                            "必须引用工具返回的真实数字，不得只写定性描述。最少 3 条。"
                            "示例：[\"close=12.34 上穿 MA20=11.80 → 均线支撑有效\","
                            "\"RSI(14)=67.2 接近超买区 → 短期追高风险\","
                            "\"2024-04-10 公告减持 500 万股 → 大股东信心不足，利空\"]"
                        ),
                        "items": {"type": "string"},
                        "minItems": 3,
                    },
                },
                "required": ["verdict", "confidence", "features", "evidence"],
            },
        },
    },
]


def _load_system_prompt() -> str:
    cfg = config.get()
    prompt_path = Path(cfg["paths"]["prompt_file"]).expanduser()
    if prompt_path.exists():
        base = prompt_path.read_text(encoding="utf-8")
    else:
        base = (
            "你是一位资深A股投资顾问。对给定股票做技术面+基本面综合分析，"
            "给出明确的判断方向和具体价位建议。分析完成后必须调用 record_verdict 工具记录结论。"
        )
    return evidence_attribution.inject_into(calibration.inject_into(base))


def _dispatch_tool(name: str, tool_input: dict) -> str:
    if name in data.TOOL_FUNCTIONS:
        return data.TOOL_FUNCTIONS[name](**tool_input)
    raise AnalysisError(f"Unknown tool: {name}")


def _make_client(cfg: dict) -> OpenAI:
    return OpenAI(
        api_key=cfg["deepseek"]["api_key"],
        base_url="https://api.deepseek.com",
    )


def _fetch_forward_bars(ts_code: str, start_d: date, end_d: date) -> list[dict]:
    """拉取 [start_d, end_d] 区间的日 K 线（升序）。失败返回 []。

    复用 data.get_daily_price，注意它会 tail(60)——所以窗口超过 60 个交易日时，
    最早的 entries 会拿不到前向收益（acceptable degradation）。
    """
    try:
        raw = data.get_daily_price(
            ts_code,
            start_date=start_d.strftime("%Y%m%d"),
            end_date=end_d.strftime("%Y%m%d"),
        )
        bars = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(bars, list):
            return []
        bars.sort(key=lambda b: str(b.get("trade_date", "")))
        return bars
    except Exception:
        return []


def _forward_return_pct(j_date: date, bars: list[dict],
                        n_trading_days: int = 10,
                        min_trading_days: int = 5) -> Optional[tuple[float, str, int]]:
    """从 j_date 起算最多 N 个交易日的收益率（%）。

    数据不足 N 天时降级到可用窗口，但至少需要 min_trading_days 天，否则返回 None。
    返回 (pct, exit_trade_date, actual_n) 或 None。
    """
    if not bars:
        return None
    j_str = j_date.strftime("%Y%m%d")
    entry_idx = None
    for i, b in enumerate(bars):
        td = str(b.get("trade_date", "")).replace("-", "")
        if td >= j_str:
            entry_idx = i
            break
    if entry_idx is None:
        return None
    available = len(bars) - 1 - entry_idx
    if available < min_trading_days:
        return None
    actual_n = min(n_trading_days, available)
    exit_idx = entry_idx + actual_n
    p0 = bars[entry_idx].get("close")
    p1 = bars[exit_idx].get("close")
    if p0 in (None, 0) or p1 is None:
        return None
    try:
        pct = (float(p1) - float(p0)) / float(p0) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return pct, str(bars[exit_idx].get("trade_date", "")), actual_n


def _match_bullish_to_closed(
    entries_sorted: list[dict],
    closed_positions: list[dict],
) -> dict[int, tuple[int, float]]:
    """多头 journal ↔ 已结仓的去重匹配（贪心，按 delta 升序）。

    每笔 closed 至多验证 1 条 bullish journal，每条 bullish journal 至多归因 1 笔 closed。
    返回 {journal_idx → (closed_idx, pnl_pct)}。
    """
    pairs: list[tuple[int, int, int, float]] = []  # (delta, j_idx, c_idx, pnl_pct)
    for j_idx, e in enumerate(entries_sorted):
        if e.get("verdict") not in BULLISH_VERDICTS:
            continue
        try:
            j_d = date.fromisoformat(e.get("date", ""))
        except (TypeError, ValueError):
            continue
        for c_idx, c in enumerate(closed_positions):
            try:
                ed = date.fromisoformat((c.get("open") or {}).get("entry_date", ""))
            except (TypeError, ValueError):
                continue
            delta = (ed - j_d).days
            if 0 <= delta <= 14:
                pnl = (c.get("close") or {}).get("realized_pnl_pct")
                if pnl is not None:
                    pairs.append((delta, j_idx, c_idx, float(pnl) * 100))
    pairs.sort(key=lambda t: t[0])
    used_j: set[int] = set()
    used_c: set[int] = set()
    out: dict[int, tuple[int, float]] = {}
    for delta, j_idx, c_idx, pnl in pairs:
        if j_idx in used_j or c_idx in used_c:
            continue
        used_j.add(j_idx)
        used_c.add(c_idx)
        out[j_idx] = (c_idx, pnl)
    return out


def _resolve_outcome_for_entry(entry: dict,
                                closed_positions: list[dict],
                                active_positions: list[dict],
                                current_price: Optional[float]) -> str:
    """B1：把一条 journal entry 关联到实际开仓结果。

    匹配规则：同 ts_code，position 的 entry_date 在 [journal.date, journal.date + 14d]。
    优先 closed（结果已知），否则 active（持仓中），否则 "未跟进"。
    """
    j_date = entry.get("date") or ""
    if not j_date:
        return ""
    try:
        j_d = date.fromisoformat(j_date)
    except (TypeError, ValueError):
        return ""

    def _within_window(e_date: str) -> bool:
        try:
            d = date.fromisoformat(e_date)
        except (TypeError, ValueError):
            return False
        return 0 <= (d - j_d).days <= 14

    # 优先匹配 closed_positions（取 entry_date 距 j_date 最近的）
    matched_closed: list[tuple[int, dict]] = []
    for c in closed_positions:
        e_date = (c.get("open") or {}).get("entry_date") or ""
        if not _within_window(e_date):
            continue
        try:
            delta = (date.fromisoformat(e_date) - j_d).days
        except Exception:
            delta = 999
        matched_closed.append((delta, c))
    if matched_closed:
        matched_closed.sort(key=lambda t: t[0])
        c = matched_closed[0][1]
        cl = c.get("close") or {}
        pnl = cl.get("realized_pnl_pct")
        days = cl.get("days_held", "?")
        reason = cl.get("exit_reason", "")
        if pnl is not None:
            return f" → **实际 {pnl * 100:+.2f}%**（持有 {days} 天，{reason}）"
        return f" → 已平仓（{reason}）"

    # 再看 active_positions
    matched_active: list[tuple[int, dict]] = []
    for p in active_positions:
        e_date = p.get("entry_date") or ""
        if not _within_window(e_date):
            continue
        try:
            delta = (date.fromisoformat(e_date) - j_d).days
        except Exception:
            delta = 999
        matched_active.append((delta, p))
    if matched_active:
        matched_active.sort(key=lambda t: t[0])
        p = matched_active[0][1]
        e_p = p.get("entry_price")
        e_date = p.get("entry_date", "?")
        if current_price is not None and e_p:
            unrealized = (float(current_price) - float(e_p)) / float(e_p) * 100
            return f" → **持仓中 {unrealized:+.2f}%**（开仓 {e_date} @ {e_p}, 现价 {current_price}）"
        return f" → 持仓中（开仓 {e_date}）"

    return " → 未跟进（未开仓）"


def _format_history(entries: list[dict], ts_code: str, limit: int = 5) -> str:
    """Format past journal entries for injection into the user prompt.

    B1: 每条历史尾部追加"实际后续表现"——从 closed/active positions 反查实际盈亏。
    """
    if not entries:
        return "（无历史记录，本次为首次分析）"

    # 一次性拉该 ts_code 的相关 closed + active positions（B1 数据源）
    closed_for_code: list[dict] = []
    active_for_code: list[dict] = []
    current_price: Optional[float] = None
    try:
        from apex import watchlist as _wl
        wl = _wl.load()
        active_for_code = [p for p in wl.get("active_positions", []) if p.get("ts_code") == ts_code]
        try:
            closed_for_code = [r for r in _wl.load_closed_positions() if r.get("ts_code") == ts_code]
        except Exception:
            closed_for_code = []
    except Exception:
        pass

    if active_for_code:
        try:
            rt = data.get_realtime_price([ts_code]).get(ts_code)
            current_price = rt
            if not current_price:
                current_price = data.get_latest_price([ts_code]).get(ts_code)
        except Exception:
            current_price = None

    all_entries_sorted = sorted(
        entries,
        key=lambda e: e.get("analyzed_at") or e.get("date", ""),
        reverse=True,
    )
    recent = all_entries_sorted[:limit]

    # 拉取覆盖所有 journal 日期的日 K 线，用于空头/未匹配多头的前向收益验证
    bars: list[dict] = []
    valid_dates: list[date] = []
    for e in all_entries_sorted:
        try:
            valid_dates.append(date.fromisoformat(e.get("date") or ""))
        except (TypeError, ValueError):
            continue
    if valid_dates:
        bars = _fetch_forward_bars(
            ts_code,
            min(valid_dates) - timedelta(days=5),
            max(valid_dates) + timedelta(days=30),
        )

    # 多头 ↔ 已结仓：去重 1:1 匹配
    bullish_match = _match_bullish_to_closed(all_entries_sorted, closed_for_code)
    confirmed_bullish: list[float] = [pnl for _, pnl in bullish_match.values()]

    # 空头 → 10 日前向收益（A 股不能做空，用价格本身验证 "避开后确实跌了" 的判断）
    confirmed_bearish: list[float] = []
    outcome_by_idx: dict[int, str] = {}
    for j_idx, e in enumerate(all_entries_sorted):
        v = e.get("verdict", "")
        # 多头匹配到已结仓 → 直接用实盘 pnl
        if j_idx in bullish_match:
            c_idx, pnl = bullish_match[j_idx]
            cl = closed_for_code[c_idx].get("close") or {}
            days = cl.get("days_held", "?")
            reason = cl.get("exit_reason", "")
            outcome_by_idx[j_idx] = f" → **实际 {pnl:+.2f}%**（持有 {days} 天，{reason}）"
            continue
        # 否则先看是否有持仓中匹配
        active_outcome = _resolve_outcome_for_entry(e, [], active_for_code, current_price)
        if active_outcome and "持仓中" in active_outcome:
            outcome_by_idx[j_idx] = active_outcome
            continue
        # 最后用前向收益评估方向类判断（中性/观望不算）
        if v in BULLISH_VERDICTS or v in BEARISH_VERDICTS:
            try:
                j_d = date.fromisoformat(e.get("date") or "")
            except (TypeError, ValueError):
                outcome_by_idx[j_idx] = " → 未跟进（日期缺失）"
                continue
            fr = _forward_return_pct(j_d, bars, n_trading_days=10, min_trading_days=5)
            if fr is None:
                outcome_by_idx[j_idx] = " → 未跟进（前向数据不足）"
                continue
            pct, _, actual_n = fr
            if v in BEARISH_VERDICTS:
                tag = "✅看跌命中" if pct < 0 else "❌看跌打脸"
                confirmed_bearish.append(pct)
                outcome_by_idx[j_idx] = f" → {actual_n}日后 {pct:+.2f}% {tag}"
            else:
                # 多头但未实际开仓 → 仅作为参考显示，不计入统计（避免实盘 pnl 与纸面收益混算）
                tag = "✅看涨命中" if pct > 0 else "❌看涨打脸"
                outcome_by_idx[j_idx] = f" → {actual_n}日后 {pct:+.2f}% {tag}（未跟进，纸面）"
        else:
            outcome_by_idx[j_idx] = " → 未跟进（未开仓）"

    confs_all: list[int] = []
    for e in all_entries_sorted:
        c = e.get("confidence")
        if c is not None:
            try:
                confs_all.append(int(c))
            except (TypeError, ValueError):
                pass

    stat_lines = []
    if confirmed_bullish:
        hit = sum(1 for p in confirmed_bullish if p > 0)
        med = sorted(confirmed_bullish)[len(confirmed_bullish) // 2]
        stat_lines.append(
            f"多头判断 {len(confirmed_bullish)} 笔（实盘 pnl）→ 盈利 {hit} 笔 "
            f"(命中率 {hit/len(confirmed_bullish)*100:.0f}%)，中位收益 {med:+.1f}%"
        )
    if confirmed_bearish:
        hit = sum(1 for p in confirmed_bearish if p < 0)
        med = sorted(confirmed_bearish)[len(confirmed_bearish) // 2]
        stat_lines.append(
            f"空头判断 {len(confirmed_bearish)} 次（5~10日前向收益）→ 后市下跌 {hit} 次 "
            f"(看跌命中率 {hit/len(confirmed_bearish)*100:.0f}%)，中位收益 {med:+.1f}%"
        )
    if confs_all:
        avg_conf = sum(confs_all) / len(confs_all)
        stat_lines.append(f"历史平均置信度 {avg_conf:.1f}/10（共 {len(confs_all)} 次）")

    stat_block = (
        f"### 命中率统计（多头={len(confirmed_bullish)}笔实盘 / "
        f"空头={len(confirmed_bearish)}次前向收益）\n"
        + ("\n".join(f"- {s}" for s in stat_lines) if stat_lines else "- 暂无可验证样本")
        + "\n（命中率低对置信度的影响见下方"
        "「置信度调整」表，不要在这里另算）"
        "\n\n### 近 {} 次分析记录\n".format(len(recent))
    )

    idx_by_id = {id(e): i for i, e in enumerate(all_entries_sorted)}
    lines = []
    for e in recent:
        j_idx = idx_by_id.get(id(e), -1)
        when = (e.get("analyzed_at") or e.get("date") or "?")[:16].replace("T", " ")
        verdict = e.get("verdict", "?")
        conf = e.get("confidence", "?")
        pa = e.get("price_advice") or {}
        entry_p = pa.get("entry") if pa.get("entry") is not None else "-"
        stop = pa.get("stop_loss") if pa.get("stop_loss") is not None else "-"
        target = pa.get("target") if pa.get("target") is not None else "-"
        outcome = outcome_by_idx.get(j_idx, " → 未跟进（未开仓）")
        lines.append(
            f"- {when} | {verdict} (置信度 {conf}/10) | 建议买入 {entry_p} 止损 {stop} 目标 {target}{outcome}"
        )
    return stat_block + "\n".join(lines)


def _resolve_industries_batch(ts_codes: list[str]) -> dict[str, str]:
    """B7 helper: 批量拿 ts_code → industry。1 次 tushare 调用搞定。失败返回空。"""
    if not ts_codes:
        return {}
    try:
        pro = data._tushare()
        df = pro.stock_basic(
            ts_code=",".join(ts_codes),
            fields="ts_code,industry",
        )
        if df is None or df.empty:
            return {}
        return {
            str(row["ts_code"]): str(row.get("industry", "") or "未知")
            for _, row in df.iterrows()
        }
    except Exception:
        return {}


def _format_portfolio_context(candidate_ts_code: str) -> str:
    """B7：当前持仓上下文，注入用户 prompt 让 AI 知道行业集中度 / 总风险。"""
    try:
        from apex import watchlist as _wl
        from apex import account as _account
        wl = _wl.load()
        positions = wl.get("active_positions", []) or []
        if not positions:
            return "## 你的当前持仓\n（你目前空仓，无相关性 / 集中度问题，仓位上限 = 单笔 + 总风险约束）"

        # 批量拉行业（持仓 + 候选）
        all_codes = list({p.get("ts_code") for p in positions if p.get("ts_code")})
        all_codes.append(candidate_ts_code)
        industries = _resolve_industries_batch(all_codes)
        candidate_industry = industries.get(candidate_ts_code, "未知")

        # 行业分布
        ind_count: dict[str, int] = {}
        for p in positions:
            ts = p.get("ts_code")
            ind = industries.get(ts, "未知") or "未知"
            ind_count[ind] = ind_count.get(ind, 0) + 1
        ind_dist = ", ".join(
            f"{ind}×{cnt}" for ind, cnt in
            sorted(ind_count.items(), key=lambda kv: -kv[1])
        )

        # 总风险 + 资金占用
        account = _account.load()
        risk_summary = _account.current_total_risk(positions, account=account)
        capital = float(account.get("total_capital") or 0)
        total_capital_used = sum(
            float((p.get("position_size_shares") or 0))
            * float(p.get("entry_price") or 0)
            for p in positions
        )

        # 同行业持仓
        same_ind_codes = [
            p.get("ts_code") for p in positions
            if industries.get(p.get("ts_code"), "") == candidate_industry
            and candidate_industry and candidate_industry != "未知"
        ]

        lines = [
            f"## 你的当前持仓上下文（{len(positions)} 只）",
            f"- 行业分布：{ind_dist}",
        ]
        if capital > 0:
            lines.append(
                f"- 资金占用：{total_capital_used:,.0f} / {capital:,.0f} "
                f"（{total_capital_used / capital * 100:.1f}%）"
            )
        if risk_summary.get("total_risk_pct") is not None:
            limit_pct = risk_summary["max_total_risk_pct"]
            lines.append(
                f"- 总风险敞口：{risk_summary['total_risk_amount']:,.0f} 元 "
                f"({risk_summary['total_risk_pct']:.2f}% 占账户) — 上限 {limit_pct}%"
                + (" **⚠ 已超上限**" if risk_summary.get("over_limit") else "")
            )
        if candidate_industry and candidate_industry != "未知":
            if same_ind_codes:
                lines.append(
                    f"- ⚠ **本次分析的 {candidate_ts_code} 行业是 {candidate_industry}，"
                    f"你已持有同行业 {len(same_ind_codes)} 只**：{', '.join(same_ind_codes)}。"
                    f"若继续加仓需明确说明：是否会推高行业集中度风险？同行业持仓相关性高，"
                    f"系统性风险（行业 -10%）可能让多个持仓同时打止损。"
                )
            else:
                lines.append(
                    f"- 本次分析 {candidate_ts_code} 行业 {candidate_industry}，"
                    f"与现有持仓无重叠（分散度 OK）。"
                )

        lines.append(
            "\n**判断时必须考虑**："
            "(a) 是否推高行业集中度？(b) 总风险是否还有余量？(c) 与现有持仓是对冲还是重叠？"
            "若加仓后会突破单一行业 ≥3 只或总风险逼近上限，结论里必须明确建议"
            "**减仓 / 等待 / 替换持仓**而不是无脑加（对置信度的影响见下方调整表）。"
        )
        return "\n".join(lines)
    except Exception as e:
        return f"## 你的当前持仓上下文\n（加载失败: {type(e).__name__}: {e}）"


def _format_market_context(ts_code: str) -> tuple[str, dict]:
    """大盘 / 板块 / 资金面 / 个股相对强度 context。

    返回 (formatted_str, raw_dict)：前者塞 prompt，后者存 journal。
    设计原则：把判断逻辑（强势 / 弱势 / 跑赢）先在 Python 里算成 regime 标签，
    AI 看到的是已经翻译过的结论而不是一堆数字，减小误读概率。
    """
    try:
        raw = data.get_market_context(ts_code)
        ctx = json.loads(raw)
    except Exception as e:
        return f"## 市场 context\n（加载失败: {type(e).__name__}: {e}）", {}

    if not ctx.get("indices") and not ctx.get("sector") and not ctx.get("north_money"):
        return "## 市场 context\n（数据全部加载失败，本次分析不可用此项）", ctx

    lines = [f"## 市场 / 板块 / 资金面 context（截至 {ctx.get('as_of', '?')}）"]

    # 大盘
    if ctx.get("indices"):
        lines.append("\n### 大盘指数")
        for idx in ctx["indices"]:
            parts = [f"{idx['name']} {idx['close']}"]
            if idx.get("daily_chg_pct") is not None:
                parts.append(f"今日 {idx['daily_chg_pct']:+.2f}%")
            if idx.get("chg_5d_pct") is not None:
                parts.append(f"5日 {idx['chg_5d_pct']:+.2f}%")
            if idx.get("chg_20d_pct") is not None:
                parts.append(f"20日 {idx['chg_20d_pct']:+.2f}%")
            if idx.get("position_60d_pct") is not None:
                parts.append(f"60日位置 {idx['position_60d_pct']:.0f}%")
            if idx.get("vol_ratio_5d") is not None:
                v = idx["vol_ratio_5d"]
                vol_label = "放量" if v > 1.2 else ("缩量" if v < 0.8 else "平量")
                parts.append(f"量比 {v:.2f}({vol_label})")
            lines.append(f"- {' / '.join(parts)}")

    # 板块
    sector = ctx.get("sector")
    if sector:
        parts = []
        if sector.get("daily_chg_pct") is not None:
            parts.append(f"今日 {sector['daily_chg_pct']:+.2f}%")
        if sector.get("chg_5d_pct") is not None:
            parts.append(f"5日 {sector['chg_5d_pct']:+.2f}%")
        if sector.get("chg_20d_pct") is not None:
            parts.append(f"20日 {sector['chg_20d_pct']:+.2f}%")
        lines.append(f"\n### 个股所属板块（{sector.get('name', '?')}）")
        lines.append(f"- {' / '.join(parts) if parts else '（数据缺失）'}")
    else:
        lines.append("\n### 个股所属板块\n- （未能匹配申万 L1 行业，跳过；可能 tushare 权限不足）")

    # 个股相对
    sr = ctx.get("stock_relative")
    if sr:
        lines.append(f"\n### 个股 {ts_code} 相对强度")
        self_parts = []
        if sr.get("chg_5d_pct") is not None:
            self_parts.append(f"5日 {sr['chg_5d_pct']:+.2f}%")
        if sr.get("chg_20d_pct") is not None:
            self_parts.append(f"20日 {sr['chg_20d_pct']:+.2f}%")
        if self_parts:
            lines.append(f"- 自身涨幅：{' / '.join(self_parts)}")
        if sr.get("vs_index_5d_pct") is not None:
            diff = sr["vs_index_5d_pct"]
            label = "跑赢" if diff > 0 else "跑输"
            lines.append(f"- vs {sr.get('ref_index_name', '大盘')} 5日：{label} {abs(diff):.2f}%")
        if sr.get("vs_sector_5d_pct") is not None:
            diff = sr["vs_sector_5d_pct"]
            label = "跑赢" if diff > 0 else "跑输"
            lines.append(f"- vs {sr.get('sector_name', '板块')} 5日：{label} {abs(diff):.2f}%")

    # 北向
    nm = ctx.get("north_money")
    if nm:
        parts = []
        if nm.get("today_yi") is not None:
            t = nm["today_yi"]
            parts.append(f"今日{'净流入' if t >= 0 else '净流出'} {abs(t):.2f} 亿")
        if nm.get("cumulative_5d_yi") is not None:
            c = nm["cumulative_5d_yi"]
            parts.append(f"5日累计{'净流入' if c >= 0 else '净流出'} {abs(c):.2f} 亿")
        if parts:
            lines.append("\n### 资金面（北向）")
            lines.append(f"- {' / '.join(parts)}")

    # 综合 regime 标签 —— 把判断写死在 Python 里，AI 直接读结论
    lines.append("\n### 综合 regime 判断（Python 预计算，直接引用）")
    regime: list[str] = []

    hs300 = next((i for i in ctx.get("indices", []) if i["code"] == "000300.SH"), None)
    hs300_5d = hs300.get("chg_5d_pct") if hs300 else None
    if hs300_5d is not None:
        if hs300_5d < -2:
            regime.append("**大盘弱势**(沪深300 5日 < -2%)")
        elif hs300_5d > 2:
            regime.append("**大盘强势**(沪深300 5日 > +2%)")
        else:
            regime.append("大盘震荡")

    sector_5d = sector.get("chg_5d_pct") if sector else None
    if sector_5d is not None and hs300_5d is not None:
        rel = sector_5d - hs300_5d
        if rel > 1:
            regime.append(f"**板块强势**({sector['name']}跑赢大盘 {rel:+.1f}%)")
        elif rel < -1:
            regime.append(f"**板块弱势**({sector['name']}跑输大盘 {rel:+.1f}%)")
        else:
            regime.append(f"板块同步({sector['name']})")

    if sr and sr.get("vs_sector_5d_pct") is not None:
        rel = sr["vs_sector_5d_pct"]
        if rel > 1.5:
            regime.append(f"**个股强于板块**({rel:+.1f}%)")
        elif rel < -1.5:
            regime.append(f"**个股弱于板块**({rel:+.1f}% — 板块涨它不涨是危险信号)")

    if nm and nm.get("cumulative_5d_yi") is not None:
        c = nm["cumulative_5d_yi"]
        if c < -100:
            regime.append(f"**北向 5 日大幅净流出** ({c:.0f} 亿)")
        elif c > 100:
            regime.append(f"**北向 5 日大幅净流入** (+{c:.0f} 亿)")

    if regime:
        lines.append("- " + " / ".join(regime))
    else:
        lines.append("- （数据不足，无法形成结论）")

    lines.append(
        "\n**裁判结论必须明确引用以上 regime 标签**；"
        "对置信度的调整见下方「置信度调整」表，不要在这里另算。"
    )

    return "\n".join(lines), ctx


def run(ts_code: str, save: bool = True, on_progress=None) -> dict:
    """
    Run full agent analysis for ts_code via DeepSeek API.
    on_progress(event: dict) is called for each milestone (context injection /
    AI assistant text / tool call / tool result / verdict). See apex/trace.py
    for event schema. Returns the parsed verdict dict. Saves to journal if save=True.
    """
    cfg = config.get()
    model = cfg["deepseek"]["model"]
    max_iter = cfg["deepseek"]["max_tool_iterations"]
    client = _make_client(cfg)
    system = _load_system_prompt()

    events: list[dict] = []

    def _emit(event: dict) -> None:
        events.append(event)
        if on_progress:
            try:
                on_progress(event)
            except Exception:
                pass

    history_block = _format_history(
        journal.load_entries(ts_code=ts_code), ts_code=ts_code,
    )
    portfolio_block = _format_portfolio_context(ts_code)
    market_block, market_ctx = _format_market_context(ts_code)
    _emit({"type": "context", "name": "history", "content": history_block})
    _emit({"type": "context", "name": "portfolio", "content": portfolio_block})
    _emit({"type": "context", "name": "market", "content": market_block})

    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"请分析股票 {ts_code}。\n\n"
                f"## 该标的过往判断（最近 5 次，含实际后续表现）\n{history_block}\n\n"
                f"{portfolio_block}\n\n"
                f"{market_block}\n\n"
                "步骤：\n"
                "1) 数据：调用 get_daily_price + get_fundamentals + get_stock_info\n"
                "\n"
                "   **结构化补充工具（推荐使用，但非强制）**：\n"
                "   · get_unlock_schedule — 限售解禁日程；多头判断前建议查，短期大额解禁是关键利空\n"
                "   · get_dragon_tiger_list — 龙虎榜上榜情况；用于判断游资炒作 / 机构动向\n"
                "   这两个工具的返回是结构化数字，比 web_search 召回的新闻 snippet 可直接引用，"
                "   优先用它们的数据填 evidence；web_search 仍负责覆盖广度。\n"
                "\n"
                "2) 博查 4 类强制（缺一类 record_verdict 被拒）：earnings / shareholders / regulatory / money_flow。\n"
                "   按需加 corporate_actions / research / industry / general。若召回为空，evidence 里明确写「该类别无召回」，**不要跳过调用**。\n"
                "\n"
                "3) 三段式辩论（写在 message content 里）：\n"
                "   ### 一、多头论点（≥3 条，格式：数据点 → 推论）\n"
                "   引用 K 线/基本面/消息面的具体数字，禁空话。\n"
                "   ### 二、空头论点（≥3 条，禁「虽然 X 但是 Y」）\n"
                "   独立反方证据；至少 1 条直接反驳多头第 N 条；必须考虑估值/解禁减持/行业景气/技术背离/历史回撤。\n"
                "   ### 三、裁判结论\n"
                "   多空各自最硬的 1 条；互斥矛盾点 → 倾向哪边？为什么？\n"
                "   最终 verdict + initial_confidence (1-10)\n"
                "\n"
                "4) **置信度调整（一次性结算）**：以 initial_confidence 为基准，遍历下表逐条结算。\n"
                "   最终 final_confidence = clamp(initial − 扣减总和 + 加分总和, 1, 10)。\n"
                "   在裁判结论里逐条列出命中的规则与具体数额。\n"
                "\n"
                "   | 条件 | 调整 |\n"
                "   |---|---|\n"
                "   | 历史命中率 < 50% 或中位收益为负 | **−2** |\n"
                "   | 每条「空头论点未被第三段有效反驳」 | **−1/条** |\n"
                "   | 加仓突破行业集中度（≥3 同行业）或总风险逼近上限 | **−1** |\n"
                "   | 多头判断 + 大盘弱势（沪深300 5日 < −2%） | **−1** |\n"
                "   | 多头判断 + 板块跑输大盘（5日 差 < −1%） | **−1** |\n"
                "   | 多头判断 + 个股跑输板块（5日 差 < −1.5%） | **−1** |\n"
                "   | 多头判断 + 板块强于大盘（5日 差 ≥ +1.5%） | **+1** |\n"
                "   | 多头判断 + 业绩超预期（最近季度净利润 yoy ≥ +30%）且 PE_TTM ≤ 30 | **+1** |\n"
                "   | 空头判断 + ST/退市风险 或 监管立案/处罚 | **+1** |\n"
                "   | 空头判断 + regime 弱势（任一 regime 利空命中） | 顺势，不扣不加 |\n"
                "\n"
                "5) 调 record_verdict：confidence 填 final_confidence；evidence ≥3 条，格式「数据点 → 推论」，引用真实数字。"
            ),
        },
    ]

    verdict_data: dict = {}
    analysis_text = ""
    iteration = 0
    searches_performed: list[str] = []  # 累计调用过的 web_search category

    while True:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            max_tokens=16384,
            temperature=0.4,
        )

        choice = response.choices[0]
        msg = choice.message

        # Collect text content + emit as event
        if msg.content:
            analysis_text += msg.content
            _emit({
                "type": "assistant_text",
                "iteration": iteration,
                "content": msg.content,
            })

        # Check termination
        if choice.finish_reason != "tool_calls":
            if choice.finish_reason == "length":
                raise AnalysisError(f"AI exceeded token limit at iteration {iteration}")
            break

        if iteration >= max_iter:
            raise AnalysisError(f"Exceeded max tool iterations ({max_iter})")

        # Append assistant message (with tool_calls) to history
        messages.append(msg)

        # Execute all tool calls
        for tool_call in (msg.tool_calls or []):
            name = tool_call.function.name
            try:
                tool_input = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                tool_input = {}

            _emit({
                "type": "tool_call",
                "iteration": iteration,
                "tool_call_id": tool_call.id,
                "name": name,
                "args": tool_input,
            })

            if name == "record_verdict":
                missing = [
                    c for c in data.MANDATORY_SEARCH_CATEGORIES
                    if c not in searches_performed
                ]
                if missing:
                    # 拒绝记录结论，强制 AI 先把强制类别补齐
                    result = json.dumps({
                        "error": (
                            f"强制博查类别未全部调用，缺：{missing}。"
                            f"请先调用 web_search(category=<上述类别>) 把每个缺失项查一遍，"
                            f"再调用 record_verdict。"
                        ),
                        "missing_categories": missing,
                        "performed": searches_performed,
                    }, ensure_ascii=False)
                    _emit({
                        "type": "verdict_rejected",
                        "iteration": iteration,
                        "tool_call_id": tool_call.id,
                        "missing": missing,
                        "performed": list(searches_performed),
                    })
                else:
                    verdict_data = tool_input
                    result = "verdict recorded"
                    _emit({
                        "type": "verdict_recorded",
                        "iteration": iteration,
                        "tool_call_id": tool_call.id,
                        "verdict": verdict_data.get("verdict"),
                        "confidence": verdict_data.get("confidence"),
                    })
            else:
                if name == "web_search":
                    cat = tool_input.get("category", "general")
                    if cat not in searches_performed:
                        searches_performed.append(cat)
                try:
                    result = _dispatch_tool(name, tool_input)
                except Exception as e:
                    result = json.dumps({"error": str(e)})
                _emit({
                    "type": "tool_result",
                    "iteration": iteration,
                    "tool_call_id": tool_call.id,
                    "name": name,
                    "summary": trace_mod.summarize_tool_result(name, result),
                    "raw": result,
                })

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

        iteration += 1

        # Once verdict captured and no other pending tools, do one final text round
        if verdict_data:
            non_verdict = [tc for tc in (msg.tool_calls or []) if tc.function.name != "record_verdict"]
            if not non_verdict:
                final = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=2048,
                    temperature=0.4,
                )
                final_text = final.choices[0].message.content
                if final_text:
                    analysis_text += "\n" + final_text
                    _emit({
                        "type": "assistant_text",
                        "iteration": iteration,
                        "content": final_text,
                        "final": True,
                    })
                break

    if not verdict_data:
        raise AnalysisError("AI did not call record_verdict — no verdict captured")

    raw_verdict = verdict_data["verdict"]
    raw_confidence = verdict_data.get("confidence")
    # AI 自报置信度兜底：clamp 到 [1, 10]，避免扣减规则叠加把分扣穿
    if raw_confidence is not None:
        try:
            raw_confidence = max(1, min(10, int(raw_confidence)))
        except (TypeError, ValueError):
            raw_confidence = None
    cal_score: Optional[float] = None
    cal_explanation = ""
    if raw_confidence is not None:
        try:
            cal_score, cal_explanation = calibration.calibrate_confidence(
                int(raw_confidence), raw_verdict,
            )
            if cal_score is not None:
                cal_score = max(1.0, min(10.0, float(cal_score)))
        except Exception as e:
            cal_explanation = f"校准失败: {e}"

    now_cn = datetime.now(_TZ_CN)
    entry = {
        "ts_code": ts_code,
        "date": now_cn.date().isoformat(),
        "analyzed_at": now_cn.isoformat(timespec="seconds"),
        "verdict": raw_verdict,
        "confidence": raw_confidence,
        "calibrated_confidence": cal_score,
        "calibration_explanation": cal_explanation,
        "price_advice": {
            "entry": verdict_data.get("entry"),
            "stop_loss": verdict_data.get("stop_loss"),
            "target": verdict_data.get("target"),
        },
        "features": verdict_data.get("features", {}),
        "evidence": verdict_data.get("evidence", []),
        "searches_performed": searches_performed,
        "market_context": market_ctx,
        "analysis_text": analysis_text.strip(),
        "prompt_version": "2.5.0",
        "source": "standalone",
    }

    if save:
        journal.write_entry(entry)
        try:
            trace_mod.write_trace(ts_code, entry["analyzed_at"], events)
        except Exception as e:
            print(f"⚠ trace 写入失败（不影响 journal）: {e}")
        print(f"✓ 已保存到日志: {ts_code} → {entry['verdict']} (置信度 {entry['confidence']})")

    entry["_trace_events"] = events  # 当前会话直接用，不序列化到 journal
    return entry
