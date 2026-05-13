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

from apex import config, data, journal, calibration
from apex.schemas import VERDICT_ENUM


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
                "用博查搜索该股票的最新新闻、公告、研报、行业动态。"
                "返回结构化结果列表（title/url/snippet/date/site），已按信任度排序"
                "（巨潮/上交所/深交所 > 东财/同花顺/雪球 > 其他）。\n"
                "**建议多次调用，分角度搜索**，例如：\n"
                "  · query='公司名 业绩 营收 净利润' freshness=oneMonth  — 业绩面\n"
                "  · query='公司名 减持 增持 大宗交易' freshness=oneMonth  — 股东动态\n"
                "  · query='公司名 公告 定增 回购 诉讼' freshness=oneMonth  — 重大事项\n"
                "  · query='行业名 政策 景气' freshness=oneMonth            — 行业面\n"
                "注意：外部文本仅供情绪面参考，高信任来源（巨潮/公告）权重高于自媒体。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码，如 002050.SZ"},
                    "name": {"type": "string", "description": "公司名（可选，用于增强搜索词）"},
                    "query": {"type": "string", "description": "自定义搜索词；留空则自动拼接「公司名 公告 研报 新闻」"},
                    "freshness": {
                        "type": "string",
                        "enum": ["oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"],
                        "description": "新鲜度，默认 oneMonth；公告/减持等敏感事项建议 oneMonth，行业政策可 oneYear",
                    },
                    "count": {"type": "integer", "description": "返回条数，默认 10"},
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
    return calibration.inject_into(base)


def _dispatch_tool(name: str, tool_input: dict) -> str:
    if name in data.TOOL_FUNCTIONS:
        return data.TOOL_FUNCTIONS[name](**tool_input)
    raise AnalysisError(f"Unknown tool: {name}")


def _make_client(cfg: dict) -> OpenAI:
    return OpenAI(
        api_key=cfg["deepseek"]["api_key"],
        base_url="https://api.deepseek.com",
    )


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

    # 命中率统计：只用已结仓的可验证记录（outcome 含"实际 +/-X%"）
    bullish_verdicts = ["bullish", "strong_bullish"]
    bearish_verdicts = ["bearish", "strong_bearish"]
    confirmed_bullish: list[float] = []   # 多头判断 + 已结仓 pnl
    confirmed_bearish: list[float] = []   # 空头判断 + 已结仓 pnl
    confs_all: list[int] = []
    for e in all_entries_sorted:
        outcome = _resolve_outcome_for_entry(e, closed_for_code, active_for_code, current_price)
        c = e.get("confidence")
        if c is not None:
            try:
                confs_all.append(int(c))
            except (TypeError, ValueError):
                pass
        # 只统计能解析到数字的已结仓条目
        import re as _re
        m = _re.search(r"实际\s*([+-]?\d+\.?\d*)%", outcome)
        if not m:
            continue
        pnl = float(m.group(1))
        v = e.get("verdict", "")
        if v in bullish_verdicts:
            confirmed_bullish.append(pnl)
        elif v in bearish_verdicts:
            confirmed_bearish.append(pnl)

    stat_lines = []
    total_verified = len(confirmed_bullish) + len(confirmed_bearish)
    if total_verified > 0:
        if confirmed_bullish:
            hit = sum(1 for p in confirmed_bullish if p > 0)
            med = sorted(confirmed_bullish)[len(confirmed_bullish) // 2]
            stat_lines.append(
                f"多头判断 {len(confirmed_bullish)} 次 → 盈利 {hit} 次 "
                f"(命中率 {hit/len(confirmed_bullish)*100:.0f}%)，中位收益 {med:+.1f}%"
            )
        if confirmed_bearish:
            hit = sum(1 for p in confirmed_bearish if p < 0)
            med = sorted(confirmed_bearish)[len(confirmed_bearish) // 2]
            stat_lines.append(
                f"空头判断 {len(confirmed_bearish)} 次 → 亏损 {hit} 次（看跌命中）"
                f"，中位收益 {med:+.1f}%"
            )
        if confs_all:
            avg_conf = sum(confs_all) / len(confs_all)
            stat_lines.append(f"历史平均置信度 {avg_conf:.1f}/10（共 {len(confs_all)} 次）")
    stat_block = (
        "### 命中率统计（已结仓可验证 {} 次）\n".format(total_verified)
        + ("\n".join(f"- {s}" for s in stat_lines) if stat_lines else "- 暂无已结仓记录可验证")
        + "\n\n⚠️ **注意**：若命中率 < 50% 或中位收益为负，本次置信度需主动下调至少 2 分。"
        "\n\n### 近 {} 次分析记录\n".format(len(recent))
    )

    lines = []
    for e in recent:
        when = (e.get("analyzed_at") or e.get("date") or "?")[:16].replace("T", " ")
        verdict = e.get("verdict", "?")
        conf = e.get("confidence", "?")
        pa = e.get("price_advice") or {}
        entry_p = pa.get("entry") if pa.get("entry") is not None else "-"
        stop = pa.get("stop_loss") if pa.get("stop_loss") is not None else "-"
        target = pa.get("target") if pa.get("target") is not None else "-"
        outcome = _resolve_outcome_for_entry(
            e, closed_for_code, active_for_code, current_price,
        )
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
            "\n**判断建议时必须考虑**："
            "(a) 是否推高行业集中度？"
            "(b) 总风险是否还有余量（上限内）？"
            "(c) 是否与现有持仓形成对冲或重叠？"
            "若加仓后会突破单一行业 ≥3 只或总风险逼近上限，应在结论里明确建议"
            "**减仓 / 等待 / 替换持仓**而不是无脑加。"
        )
        return "\n".join(lines)
    except Exception as e:
        return f"## 你的当前持仓上下文\n（加载失败: {type(e).__name__}: {e}）"


def run(ts_code: str, save: bool = True, on_progress=None) -> dict:
    """
    Run full agent analysis for ts_code via DeepSeek API.
    on_progress(msg: str) is called at each tool call for live UI updates.
    Returns the parsed verdict dict. Saves to journal if save=True.
    """
    cfg = config.get()
    model = cfg["deepseek"]["model"]
    max_iter = cfg["deepseek"]["max_tool_iterations"]
    client = _make_client(cfg)
    system = _load_system_prompt()

    history_block = _format_history(
        journal.load_entries(ts_code=ts_code), ts_code=ts_code,
    )
    portfolio_block = _format_portfolio_context(ts_code)

    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"请分析股票 {ts_code}。\n\n"
                f"## 该标的过往判断（最近 5 次，含实际后续表现）\n{history_block}\n\n"
                f"{portfolio_block}\n\n"
                "步骤：\n"
                "1) 调用 get_daily_price + get_fundamentals + get_stock_info 获取基础数据；\n"
                "2) **多角度博查搜索（必须至少调用 2 次 web_search，查不同主题）**：\n"
                "   · 第 1 次：query='公司名 业绩 营收 净利润' freshness=oneMonth\n"
                "   · 第 2 次：query='公司名 减持 增持 定增 回购 诉讼' freshness=oneMonth\n"
                "   · 如发现重大利空/利好，可追加第 3 次：query='行业名 政策 景气' freshness=oneMonth\n"
                "   结果中 site=cninfo/sse/szse 的来源权重最高，自媒体来源仅作参考；\n"
                "3) 做完整技术面+基本面+消息面综合分析；\n"
                "4) **复盘历史判断**：上方命中率统计已给出数字。"
                "若多头命中率 < 50% 或中位收益为负，本次置信度必须主动下调至少 2 分，"
                "并在分析文字中明确说明原因；\n"
                "5) **结合持仓上下文**：参考上方持仓行业分布与总风险，"
                "若加仓会突破集中度或总风险，要在结论里明确说出来；\n"
                "6) 最后调用 record_verdict 工具，**evidence 字段必填**（≥3 条，"
                "每条必须引用工具返回的真实数字，格式：数据点 → 推论）。"
            ),
        },
    ]

    verdict_data: dict = {}
    analysis_text = ""
    iteration = 0

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

        # Collect text content
        if msg.content:
            analysis_text += msg.content

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
            except json.JSONDecodeError as e:
                tool_input = {}

            if name == "record_verdict":
                verdict_data = tool_input
                result = "verdict recorded"
                if on_progress:
                    on_progress(f"📝 记录结论: {verdict_data.get('verdict')}")
            else:
                tool_labels = {
                    "get_daily_price": f"📊 拉取K线数据 {tool_input.get('ts_code', '')}",
                    "get_fundamentals": f"🏦 拉取基本面 {tool_input.get('ts_code', '')}",
                    "get_stock_info": f"ℹ️ 查询公司信息 {tool_input.get('ts_code', '')}",
                    "web_search": f"🔍 博查搜索: {tool_input.get('query') or tool_input.get('name') or tool_input.get('ts_code', '')}",
                }
                if on_progress:
                    on_progress(tool_labels.get(name, f"🔧 {name}"))
                try:
                    result = _dispatch_tool(name, tool_input)
                except Exception as e:
                    result = json.dumps({"error": str(e)})

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
                break

    if not verdict_data:
        raise AnalysisError("AI did not call record_verdict — no verdict captured")

    raw_verdict = verdict_data["verdict"]
    raw_confidence = verdict_data.get("confidence")
    cal_score: Optional[float] = None
    cal_explanation = ""
    if raw_confidence is not None:
        try:
            cal_score, cal_explanation = calibration.calibrate_confidence(
                int(raw_confidence), raw_verdict,
            )
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
        "analysis_text": analysis_text.strip(),
        "prompt_version": "2.2.0",
        "source": "standalone",
    }

    if save:
        journal.write_entry(entry)
        print(f"✓ 已保存到日志: {ts_code} → {entry['verdict']} (置信度 {entry['confidence']})")

    return entry
