"""AI 对话接口（SSE 流式）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from apex import llm

from backend.core.streaming import stream_generator
from backend.schemas.chat import ChatStreamRequest

router = APIRouter(prefix="/chat", tags=["chat"])


_CHAT_SYSTEM_PROMPT = """\
你是 Apex AI 股票分析助手，一个 A 股投资对话机器人。

## 核心能力
- 分析 A 股市场、板块、个股
- 解读行情数据、资金流向、技术指标
- 回答投资方法论问题（PE/PB/仓位管理等）

## 意图分析（重要）
用户表达往往不完整。收到问题后先判断清晰度：

**模糊问题**（如"看看今天盘面""有色板块怎么样""最近有什么机会"）：
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


def _trim_history(history: list[dict], budget: int) -> list[dict]:
    """保留最近若干条消息使其总 token 在预算内。"""
    kept: list[dict] = []
    total = 0
    for msg in reversed(history):
        t = _estimate_tokens(msg.get("content", ""))
        if total + t > budget and kept:
            break
        kept.insert(0, msg)
        total += t
    return kept


@router.post("/stream")
def chat_stream(req: ChatStreamRequest):
    """AI 对话，SSE 流式返回回复。

    事件：
      ``chunk`` — ``{content}`` 增量文本
      ``done``  — 结束
      ``error`` — 失败
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S 北京时间")
    system = _CHAT_SYSTEM_PROMPT.format(current_time=now_str)

    messages = [{"role": "system", "content": system}]
    for m in _trim_history(req.history, _HISTORY_TOKEN_BUDGET):
        messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": req.message})

    return EventSourceResponse(stream_generator(
        lambda: llm.chat_stream(messages, temperature=0.3, max_tokens=8192)
    ))
