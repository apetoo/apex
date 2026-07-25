"""AI 个股分析接口（SSE 流式）+ 日志/trace 历史。"""
from __future__ import annotations

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

from apex import analyze as ana
from apex import data as data_mod
from apex import journal
from apex import trace as trace_mod

from backend.core.streaming import stream_callback

router = APIRouter(prefix="/analyze", tags=["analyze"])


@router.get("/run")
def run_analysis(
    ts_code: str = Query(...),
    save: bool = Query(True),
):
    """启动 AI 分析，SSE 流式返回 trace 事件 + 最终结果。

    事件：
      ``trace`` — 分析过程每一步（tool_call / tool_result / assistant_text / context ...）
      ``done``  — 最终分析结果 dict
      ``error`` — 失败
    """
    code = data_mod.normalize_ts_code(ts_code)
    return EventSourceResponse(stream_callback(ana.run, code, save=save))


# ── 日志 / trace ──────────────────────────────────────────────────────────────


journal_router = APIRouter(prefix="/journal", tags=["analyze"])


@journal_router.get("")
def list_all_journal():
    """全部标的全部历史分析（按时间倒序）。供 /journal 跨股历史页。

    ``journal.load_entries()`` 读所有 *.jsonl, **含 position_action**（持仓加减仓建议也是历史记录,
    用户要在历史页看到）。量级 ~数百条, 直接全量返回(前端本地搜索)。

    老条目缺 ``name``(中文名), 这里用全量 name map 兜底回填, 供列表展示。
    """
    entries = journal.load_entries()
    name_map = data_mod.get_name_map()
    if name_map:
        for e in entries:
            if not e.get("name"):
                n = name_map.get(e.get("ts_code", ""))
                if n:
                    e["name"] = n
    entries.sort(
        key=lambda e: e.get("analyzed_at") or e.get("date", ""),
        reverse=True,
    )
    return entries


@journal_router.get("/{ts_code}")
def list_journal(ts_code: str):
    """该股票全部历史分析（按时间倒序，含 position_action 持仓建议）。"""
    code = data_mod.normalize_ts_code(ts_code)
    entries = journal.load_entries(code)
    entries.sort(
        key=lambda e: e.get("analyzed_at") or e.get("date", ""),
        reverse=True,
    )
    return entries


@journal_router.get("/{ts_code}/latest")
def latest_journal(ts_code: str):
    """最近一条分析记录（含 position_action）。供"同步AI到持仓"取最近 advice：
    持仓后最新可能是 position_action（new_stop），前端按 source 分流取 new_stop / price_advice。
    无记录返回 null。
    """
    return journal.load_latest(data_mod.normalize_ts_code(ts_code))


trace_router = APIRouter(prefix="/trace", tags=["analyze"])


@trace_router.get("/{ts_code}/{analyzed_at}")
def get_trace(ts_code: str, analyzed_at: str):
    """某次分析的过程记录（events 列表）。无记录返回 null。"""
    return trace_mod.load_trace(data_mod.normalize_ts_code(ts_code), analyzed_at)
