"""AI 对话接口（SSE 流式）。

定位：用户投资助理 + 复盘助手（非"又一个分析股票的 AI"，深度分析走 analyze 页面）。
通过 function-calling 让模型按需拉取用户行为数据（持仓/平仓/胜率/证据归因）再回答。

两阶段流式（参考 analyze agent 的工具循环，但 chat 只跑单轮）：
  阶段1：``llm.chat(tools=, tool_choice="auto")`` 非流式决策——模型决定查不查工具。
        有 tool_calls → 派发 → 结果以 role:tool 回填 messages。
        无 tool_calls → 缓存 content 作"假流式"前缀。
  阶段2：``llm.chat_stream`` 流式生成最终回复（不带工具）。
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from apex import account, calibration, evidence_attribution, journal, llm, trades, watchlist
from apex.journal_views import chat_full, chat_summary, is_today_entry
from apex.review_views import (
    account_risk_digest,
    calibration_digest,
    closed_trades_digest,
    evidence_digest,
    position_size_digest,
    trade_diagnosis_digest,
    trade_history_digest,
    watchlist_digest,
)

from backend.core.streaming import stream_generator
from backend.schemas.chat import ChatStreamRequest

router = APIRouter(prefix="/chat", tags=["chat"])


_CHAT_SYSTEM_PROMPT = """\
你是 Apex AI 投资助理，定位是**用户的交易复盘助手**——帮用户回顾自己的判断与操作、
发现自己的交易模式，而非"又一个分析股票的 AI"（深度个股分析由专门的 analyze 页面负责，
chat 不重复跑分析）。

## 核心能力
- 复盘用户已平仓交易：哪些判断对了/错了、胜率、盈亏来源
- 解读用户的胜率校准表、证据归因（哪类信号对该用户预测准/是噪音）
- 基于用户当前持仓/候选给操作建议
- 回答投资方法论问题（PE/PB/仓位管理/止损纪律等）

## 工具使用（重要 — 用户行为数据挖掘）
用户问以下问题时**优先调对应工具拿真实数据**，不要凭记忆/臆测回答：

- "我手里有什么 / 我有哪些候选 / 我持仓情况" → **get_my_watchlist**
- "我最近亏在哪 / 哪些判断错了 / 我的胜率 / 复盘一下" → **review_closed_trades**
- "哪个策略适合我 / 我高置信度准不准 / 我的胜率表" → **get_calibration_snapshot**
- "龙虎榜信号对我有用吗 / 什么证据最靠谱 / 哪类论据预测准" → **get_evidence_attribution**
- 用户消息含 A 股代码且问"之前怎么看/上次分析/历史判断" → **get_stock_analysis**
  （detail=summary 概要，detail=full 全量；返回"无分析记录"=该股未分析过，别臆造）
- "我最近买卖了什么 / 上周对某股加过仓吗 / 买卖节奏怎么样" → **get_trade_history**
  （逐笔 buy/sell 流水，含加仓/减仓/单笔止损；和 review_closed_trades 互补，前者看闭环后者看逐笔）
- "我仓位重不重 / 现在总风险敞口多少 / 超限没" → **get_account_risk**
  （仓位风控问题前优先调，不要凭持仓数瞎估总风险）
- "这只 X 块买、Y 块止损我该买多少手 / 仓位多大合适" → **compute_position_size**
  （entry+stop 必填；返回 ok=false 说明风险预算不足或参数异常，如实告知）
- "上次某股亏了到底错在哪 / 这笔交易 AI 怎么复盘的" → **get_trade_diagnosis**
  （取该股最近一笔平仓的 AI 事后诊断；返回"无诊断"=平仓时未生成，别臆造根因）

工具返回"暂无数据/样本不足"时如实告诉用户，别编造数字。复盘结论要引用具体笔数/胜率/
盈亏，不要空泛。

## 意图分析
**模糊问题**（"看看最近怎么样""有什么机会"）：
→ 先做 1 轮澄清，追问 1-2 个关键方向。
**明确问题**（含股票代码/具体指标/明确指令）：
→ 直接回答。末尾可追加 1 个扩展方向。

## 对话规则
- 回答简洁、要点式。每条结论有具体数据或逻辑支撑
- 涉及买卖建议时声明「⚠️ 仅供参考，不构成投资建议」
- 用中文回答
- 当前时间：{current_time}
"""

# 上下文预算（DeepSeek 长窗口下保留约 24K tokens 历史）
_HISTORY_TOKEN_BUDGET = 24000


def _estimate_tokens(text: str) -> int:
    """中英混排的粗略 token 估算：中文 ~1.5，ASCII ~0.3。"""
    cn = sum(1 for c in text if "一" <= c <= "鿿")
    en = len(text) - cn
    return int(cn * 1.5 + en * 0.3)


def _msg_content(msg) -> str:
    """兼容 dict 和 pydantic ChatMessage 取 content。"""
    if isinstance(msg, dict):
        return msg.get("content", "")
    return getattr(msg, "content", "") or ""


def _trim_history(history: list, budget: int) -> list:
    """保留最近若干条消息使其总 token 在预算内。

    history 元素可能是 dict(原始) 或 pydantic ChatMessage(经 schema 解析),
    两者都兼容。返回原始元素(不转换), 由调用方按需取 role/content。
    """
    kept: list = []
    total = 0
    for msg in reversed(history):
        t = _estimate_tokens(_msg_content(msg))
        if total + t > budget and kept:
            break
        kept.insert(0, msg)
        total += t
    return kept


# ── 工具表 ────────────────────────────────────────────────────────────────────
# 5 个工具：1 个历史分析查询 + 4 个用户行为/复盘挖掘。全部只读。

_CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_analysis",
            "description": (
                "查询当前讨论股票的历史 AI 分析记录。当用户消息含 A 股代码且需要参考过往判断时调用。"
                "detail=summary 返回最近一次概要(判决+建议价+2条证据, 约250 tokens); "
                "detail=full 返回全量(含完整证据/特征/叙述)。默认 summary。"
                "返回『无分析记录』表示该股未分析过。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "A 股代码, 如 600309.SH"},
                    "detail": {
                        "type": "string",
                        "enum": ["summary", "full"],
                        "default": "summary",
                    },
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_watchlist",
            "description": (
                "查询用户当前持仓、候选(待触发)、归档列表。用户问『我手里有什么/我有哪些候选/"
                "我持仓情况/我现在持有』时调用。让助手了解用户当前盘面。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "review_closed_trades",
            "description": (
                "复盘用户已平仓交易：列出每笔的 AI 当时判决→实际盈亏→退出原因, 并汇总胜率/"
                "平均盈亏/最大赚亏。用户问『我最近亏在哪/哪些判断错了/我的胜率/复盘一下/"
                "我交易表现如何』时调用。这是 chat 的核心复盘能力。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "返回最近多少笔, 默认 20",
                        "default": 20,
                    },
                    "since_days": {
                        "type": "integer",
                        "description": "只看最近 N 天内平仓的(与 limit 二选一)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_calibration_snapshot",
            "description": (
                "查询用户的历史胜率校准表(按 verdict×置信度桶/策略/regime 维度)。"
                "用户问『哪个策略适合我/我高置信度准不准/我的胜率表/看多看空各自胜率』时调用。"
                "样本不足(每桶<3笔)时如实返回。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_evidence_attribution",
            "description": (
                "查询用户的证据归因: 哪类论据(如龙虎榜/解禁/板块强势)对该用户预测准、哪类是噪音。"
                "用户问『龙虎榜信号对我有用吗/什么证据最靠谱/哪类论据预测准』时调用。"
                "这是帮用户发现自己的认知偏差的工具。样本不足时如实返回。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trade_history",
            "description": (
                "查询用户逐笔买卖流水(trades.jsonl: 每一笔 buy/sell, 含加仓/减仓/部分平仓/单笔止损)。"
                "用户问『我最近买卖了什么/上周对某股加过仓吗/最近卖了多少笔/某只股我的买卖节奏』时调用。"
                "与 review_closed_trades 互补: 后者看闭环交易的判决→盈亏, 本工具看原始逐笔流水。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "可选, 按标的过滤, 如 600519.SH"},
                    "limit": {"type": "integer", "description": "返回最近多少笔, 默认 20", "default": 20},
                    "since_days": {"type": "integer", "description": "只看最近 N 天内(与 limit 可组合)"},
                    "side": {"type": "string", "enum": ["buy", "sell"], "description": "可选, 只看买或卖"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_account_risk",
            "description": (
                "查询账户当前总风险敞口: 所有持仓未实现风险合计、占账户百分比、是否超总风险上限。"
                "用户问『我仓位重不重/现在风险敞口多大/总风险超限没』时调用。"
                "回答仓位风控问题前优先调此工具拿真实数字, 不要凭持仓数瞎估。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_position_size",
            "description": (
                "仓位计算器: 给定进场价和止损价, 按账户风险比例模型算出建议手数、承担风险金额、所需资金。"
                "用户问『这只 X 块买、Y 块止损我该买多少手/仓位多大合适』时调用。"
                "返回 ok=false 时说明风险预算不足或参数异常, 应如实告知用户。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entry": {"type": "number", "description": "进场价"},
                    "stop": {"type": "number", "description": "止损价"},
                    "risk_pct": {"type": "number", "description": "可选, 单笔风险比例(%), 默认用账户配置"},
                },
                "required": ["entry", "stop"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trade_diagnosis",
            "description": (
                "查询某笔已平仓交易的 AI 事后诊断全文: AI 当时判断对的地方、漏掉/错的地方、教训、诊断叙述。"
                "用户问『我上次某股亏了到底错在哪/这笔交易 AI 怎么复盘的』时调用。"
                "默认取该股最近一笔平仓; 传 closed_at 精确定位某笔。返回『无诊断』表示平仓时未生成。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "A 股代码, 如 600519.SH"},
                    "closed_at": {"type": "string", "description": "可选, 精确定位某笔平仓的 closed_at"},
                },
                "required": ["ts_code"],
            },
        },
    },
]


def _dispatch_chat_tool(name: str, args: dict) -> str:
    """执行 chat 工具, 返回结果字符串(塞回 tool message)。失败也返回 JSON 错误串, 不崩流。"""
    try:
        if name == "get_stock_analysis":
            ts_code = args.get("ts_code", "")
            detail = args.get("detail", "summary")
            if not ts_code:
                return json.dumps({"error": "ts_code 不能为空"}, ensure_ascii=False)
            latest = journal.load_latest(ts_code)
            if not latest:
                return json.dumps({"error": "无分析记录", "ts_code": ts_code}, ensure_ascii=False)
            today = is_today_entry(latest)
            view = chat_full(latest, is_today=today) if detail == "full" else chat_summary(latest, is_today=today)
            return view

        if name == "get_my_watchlist":
            return watchlist_digest(watchlist.load())

        if name == "review_closed_trades":
            limit = args.get("limit") or 20
            since_days = args.get("since_days")
            recs = watchlist.load_closed_positions(limit=limit, since_days=since_days)
            return closed_trades_digest(recs, limit=limit)

        if name == "get_calibration_snapshot":
            return calibration_digest(calibration.load())

        if name == "get_evidence_attribution":
            return evidence_digest(evidence_attribution.load())

        if name == "get_trade_history":
            ts_code = args.get("ts_code") or None
            limit = args.get("limit") or 20
            since_days = args.get("since_days")
            side = args.get("side")
            recs = trades.load_trades(ts_code=ts_code, limit=limit, since_days=since_days)
            if side:
                recs = [r for r in recs if r.get("side") == side]
            return trade_history_digest(recs)

        if name == "get_account_risk":
            actives = watchlist.load().get("active_positions") or []
            return account_risk_digest(account.current_total_risk(actives), account.load())

        if name == "compute_position_size":
            entry = args.get("entry")
            stop = args.get("stop")
            if entry is None or stop is None:
                return json.dumps({"error": "entry 和 stop 必填"}, ensure_ascii=False)
            ps = account.compute_position_size(
                entry=float(entry), stop=float(stop), risk_pct=args.get("risk_pct")
            )
            return position_size_digest(ps)

        if name == "get_trade_diagnosis":
            ts_code = args.get("ts_code", "")
            if not ts_code:
                return json.dumps({"error": "ts_code 不能为空"}, ensure_ascii=False)
            closed_at = args.get("closed_at")
            recs = watchlist.load_closed_positions()
            target = None
            if closed_at:
                target = next(
                    (r for r in recs if (r.get("close") or {}).get("closed_at") == closed_at),
                    None,
                )
            else:
                matching = [r for r in recs if r.get("ts_code") == ts_code]
                matching.sort(
                    key=lambda r: (r.get("close") or {}).get("closed_at") or "",
                    reverse=True,
                )
                target = matching[0] if matching else None
            if not target:
                return json.dumps({"error": "无该股平仓记录", "ts_code": ts_code}, ensure_ascii=False)
            return trade_diagnosis_digest(target)

        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 — 工具失败不崩流, 透传给模型
        return json.dumps({"error": f"工具 {name} 执行失败: {exc}"}, ensure_ascii=False)


@router.post("/stream")
def chat_stream(req: ChatStreamRequest):
    """AI 对话，SSE 流式返回回复。

    事件：
      ``chunk`` — ``{content}`` 增量文本
      ``done``  — 结束
      ``error`` — 失败

    两阶段：阶段1 非流式带工具(tool_choice=auto)让模型自己决定查不查用户数据;
    阶段2 流式生成最终回复(无工具)。模型决定不查工具时, 阶段1 已拿到的 content
    作为"假流式"前缀先吐, 再续写。
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S 北京时间")
    system = _CHAT_SYSTEM_PROMPT.format(current_time=now_str)

    messages: list[dict] = [{"role": "system", "content": system}]
    for m in _trim_history(req.history, _HISTORY_TOKEN_BUDGET):
        role = m.get("role") if isinstance(m, dict) else m.role
        content = m.get("content") if isinstance(m, dict) else m.content
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": req.message})

    # 阶段1: 工具决策轮(总是跑)。tool_choice=auto 让模型自己决定查不查。
    try:
        tool_resp = llm.chat(
            messages, tools=_CHAT_TOOLS, tool_choice="auto",
            temperature=0.3, max_tokens=512,
        )
    except Exception:  # noqa: BLE001 — 阶段1失败降级为纯流式
        tool_resp = {"content": None, "tool_calls": None}

    tool_calls = tool_resp.get("tool_calls")
    if tool_calls:
        # 执行工具, 把调用链 + 结果 append 进 messages, 阶段2流式生成最终回复
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            result = _dispatch_chat_tool(tc["name"], args)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    # 注: 不调工具时不缓存 seed_content。阶段1 的 content 是用 max_tokens=512 截断的
    # 不完整回复, 若当"假流式前缀"先吐、阶段2 又用同一 messages 重生成 → 内容重复乱序。
    # 所以不调工具时直接走阶段2 流式(从头完整生成), 阶段1 仅用于工具决策。

    # 阶段2: 流式生成最终回复(无工具)
    def gen_factory():
        yield from llm.chat_stream(messages, temperature=0.3, max_tokens=8192)

    return EventSourceResponse(stream_generator(gen_factory))
