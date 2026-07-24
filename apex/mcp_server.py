"""apex MCP server - 让外部 agent（Codex / Claude / OpenClaw）远程指挥 apex。

契约 = apex 能力的 MCP 适配层。本文件是**纯适配器**：
- 只 import 并调现有 ``apex.*`` 函数，**不 import backend/***（守 apex↛backend 层次）。
- **不改任何现有 apex 模块**（用户确认的不变量；增强一律新文件）。
- 异常->错误 dict 映射**自定义在本文件**，不 import ``backend/core/errors.py``（MCP 无 HTTP 状态码）。
- DataFrame 序列化**自定义** ``_df_records``，不 import ``backend/core/response.py``。

传输：stdio，本机无鉴权（v2 再上 HTTP/SSE + token）。
跑法：``.venv/bin/python -m apex.mcp_server``，注册到 Claude/Codex 的 MCP 配置。

设计文档：~/.gstack/projects/apex/wanmingyu-develop-design-20260723-111429.md

v1 范围：2 静态 resource + 4 resource 模板 + 7 tool。
  analyze / screener 当前为**阻塞 async tool**（``asyncio.to_thread`` 跑分钟级阻塞调用），
  不流式进度--fastmcp 3.x 异步生成器 tool 的流式语义需专门 spike，留 v2。
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastmcp import FastMCP

# 先载入配置（token / proxy / paths），再 import 会用到 config.get() 的下游模块。
from apex import config

config.load()

from apex import (  # noqa: E402
    analyze,
    backtest,
    calibration,
    data,
    journal,
    monitor,
    postmortem as pm_mod,
    screener,
    watchlist,
)
from apex.watchlist import DuplicatePositionError, PositionNotFoundError  # noqa: E402


mcp = FastMCP("apex-trader")


# ── 辅助 ────────────────────────────────────────────────────────────────────
def _resolve_name(ts_code: str, name: Optional[str]) -> str:
    """名称缺失时反查 tushare（对齐 backend/routers/watchlist._resolve_name 行为）。

    失败返回空串--名称反查不应阻断加候选。留在 apex 层，不 import backend。
    """
    if name:
        return name
    try:
        raw = data.get_stock_info(ts_code=ts_code)
        info = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(info, list) and info:
            return info[0].get("name", "") or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def _err(code: str, **extra) -> dict:
    """统一的 MCP 错误返回形状（MCP 无 HTTP 状态码，靠 error 字段区分）。"""
    return {"error": code, **extra}


def _df_records(df) -> list[dict]:
    """DataFrame -> records 列表（NaN->null，日期 iso）。

    复用 ``backend/core/response.dataframe_records`` 的序列化方式，但在本文件内重实现，
    不 import backend。
    """
    if df is None or getattr(df, "empty", False):
        return []
    return json.loads(df.to_json(orient="records", force_ascii=False, date_format="iso"))


def _sort_journal(entries: list[dict]) -> list[dict]:
    """journal 按 analyzed_at（旧条目用 date）倒序，对齐 backend /journal/{ts_code}。"""
    entries.sort(key=lambda e: e.get("analyzed_at") or e.get("date", ""), reverse=True)
    return entries


# ── Resources：静态读 ───────────────────────────────────────────────────────
@mcp.resource("watchlist://state")
def watchlist_state() -> dict:
    """全量 watchlist：active_positions / candidates / archived。"""
    return watchlist.load()


@mcp.resource("triggers://recent")
def recent_triggers() -> dict:
    """近期触发（默认近 24h，已 ack 的不计）-- agent「听」盘中 loop 产出的入口。

    loop（``automation.loop`` + ``monitor.check_once``）已在替你盯触发并推 notify
    （console/bark）；这里只读它落盘的 triggers jsonl，不重跑监控。
    """
    return {"triggers": monitor.load_recent_triggers(hours=24, include_acked=False)}


# ── Resources：按 ts_code 模板读 ────────────────────────────────────────────
@mcp.resource("journal://{ts_code}")
def journal_history(ts_code: str) -> dict:
    """该股全部历史 AI 分析（按时间倒序）。"""
    code = data.normalize_ts_code(ts_code)
    return {"ts_code": code, "entries": _sort_journal(journal.load_entries(code))}


@mcp.resource("price://{ts_code}")
def stock_price(ts_code: str) -> dict:
    """现价：实时优先（新浪），盘中无价回退日线收盘（对齐 backend /market/prices）。"""
    code = data.normalize_ts_code(ts_code)
    price = data.get_realtime_price([code]).get(code)
    source = "realtime"
    if price is None:
        price = data.get_latest_price([code]).get(code)
        source = "daily" if price is not None else "none"
    return {"ts_code": code, "price": price, "source": source}


@mcp.resource("intraday://{ts_code}")
def intraday_bars(ts_code: str) -> dict:
    """当日分时 K 线 + VWAP 原始数据（腾讯分时源）。"""
    code = data.normalize_ts_code(ts_code)
    return data.get_intraday_bars(code, trade_date=None, with_prev_vol=False)


@mcp.resource("market://context/{ts_code}")
def market_context(ts_code: str) -> dict:
    """大盘/板块/资金面上下文 + 市场情绪 regime（注入分析用的文本，按个股取）。"""
    code = data.normalize_ts_code(ts_code)
    return {"context": data.get_market_context(code)}


# ── Tools：写/动作，带守卫 ──────────────────────────────────────────────────
@mcp.tool
def add_candidate(ts_code: str,
                  trigger_price: float,
                  name: Optional[str] = None,
                  stop_advice: Optional[float] = None,
                  target_advice: Optional[float] = None,
                  note: str = "",
                  strategy: Optional[str] = None) -> dict:
    """加一只候选（等触发买入）。``trigger_price`` = AI 建议入场价。

    ``stop_advice`` / ``target_advice`` 可选，带入候选供触发提醒上下文用。
    同 ts_code 已有候选则 **upsert**（原地替换、保留 renew_count，不报错）。
    返回 ``{ts_code, expires_days, method, ...}``。
    """
    code = data.normalize_ts_code(ts_code)
    meta = watchlist.add_candidate(
        code,
        _resolve_name(code, name),
        trigger_price,
        note=note,
        stop_advice=stop_advice,
        target_advice=target_advice,
        strategy=strategy,
    )
    return {"message": "Candidate added", "ts_code": code, **meta}


@mcp.tool
def promote_candidate(ts_code: str,
                      entry_price: float,
                      stop_loss: float,
                      target: float) -> dict:
    """候选转持仓（用**实际成交价**）。守卫已内置：先验重（持仓已存在则拒绝），再 archive 候选。

    - 候选不存在 -> ``{error: "candidate_not_found", ts_code}``
    - 持仓已存在 -> ``{error: "duplicate_position", ts_code, existing}``
    成功返回 ``{message, ts_code, position}``。
    """
    code = data.normalize_ts_code(ts_code)
    try:
        record = watchlist.promote_candidate(
            ts_code=code,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target=target,
        )
        return {"message": "Candidate promoted", "ts_code": code, "position": record}
    except DuplicatePositionError as e:
        return _err("duplicate_position", ts_code=e.ts_code, existing=e.existing)
    except ValueError as e:
        # promote_candidate 在候选不存在时抛 ValueError
        return _err("candidate_not_found", ts_code=code, detail=str(e))


@mcp.tool
async def close_position(ts_code: str,
                         exit_price: float,
                         exit_reason: str,
                         exit_date: Optional[str] = None,
                         user_notes: Optional[str] = None,
                         actual_fill_price: Optional[float] = None,
                         postmortem: bool = True) -> dict:
    """平仓。``postmortem=True``（默认）串联 AI 复盘 + 校准重算（对齐 backend /watchlist/close）。

    - 持仓不存在 -> ``{error: "position_not_found", ts_code}``
    - postmortem/calibration 失败不阻断平仓（diagnosis 带 error）。
    返回 ``{message, ts_code, record, diagnosis}``。postmortem 部分跑 AI，可能慢（走线程）。
    """
    code = data.normalize_ts_code(ts_code)
    try:
        rec = watchlist.close_position(
            ts_code=code,
            exit_price=exit_price,
            exit_reason=exit_reason,
            exit_date=exit_date,
            user_notes=user_notes,
            actual_fill_price=actual_fill_price,
        )
    except PositionNotFoundError as e:
        return _err("position_not_found", ts_code=code, detail=str(e))

    diagnosis = None
    if postmortem:
        def _pm() -> Optional[dict]:
            d = pm_mod.run_and_patch(rec)
            try:
                calibration.compute()
            except Exception:  # noqa: BLE001 - 校准失败不阻断
                pass
            return d

        try:
            diagnosis = await asyncio.to_thread(_pm)
        except Exception as e:  # noqa: BLE001 - 复盘失败不阻断平仓
            diagnosis = {"error": "postmortem_failed", "detail": str(e)}
    return {"message": "Position closed", "ts_code": code, "record": rec, "diagnosis": diagnosis}


@mcp.tool
def archive_entry(ts_code: str,
                  section: str = "candidates",
                  reason: str = "archived_manual") -> dict:
    """软删除（写 ``status=archived_<reason>``）。section 默认 candidates，reason 默认 manual。"""
    code = data.normalize_ts_code(ts_code)
    moved = watchlist.archive_entry(ts_code=code, section=section, reason=reason)
    return {"message": "Entry archived" if moved else "Entry not found",
            "ts_code": code, "moved": moved}


@mcp.tool
async def analyze_stock(ts_code: str, save: bool = True) -> dict:
    """跑一次 DeepSeek 个股分析（**阻塞，可能数分钟**；v2 改流式进度）。

    返回 ``{ts_code, verdict}``；失败 ``{error: "analysis_failed", ts_code, detail}``。
    on_progress 传 no-op（不消费 trace 事件；如需 trace 走 backend SSE 端点）。
    """
    code = data.normalize_ts_code(ts_code)
    try:
        verdict = await asyncio.to_thread(
            analyze.run, code, save=save, on_progress=lambda _e: None
        )
        return {"ts_code": code, "verdict": verdict}
    except Exception as e:  # noqa: BLE001
        return _err("analysis_failed", ts_code=code, detail=str(e))


@mcp.tool
async def run_screener(skip_ai: bool = False,
                       skip_selector: bool = False,
                       strategy_weights: Optional[dict] = None) -> dict:
    """跑今日粗筛（**阻塞，开 AI 时可能数分钟**；v2 改流式进度）。

    ``strategy_weights`` 留空=等权。返回 ``{report}``；失败 ``{error: "screener_failed", detail}``。
    """
    try:
        report = await asyncio.to_thread(
            screener.run,
            skip_ai=skip_ai,
            skip_selector=skip_selector,
            strategy_weights=strategy_weights,
            on_progress=lambda _m: None,
        )
        return {"report": report}
    except Exception as e:  # noqa: BLE001
        return _err("screener_failed", detail=str(e))


@mcp.tool
def run_backtest(ts_code: Optional[str] = None, lookforward_days: int = 10) -> dict:
    """信号模拟回测：以看多 verdict 为信号，T+1 开盘入场，逐笔 P&L。

    ``ts_code`` 留空=全部。返回 ``{ts_code, signals: [...]}``（DataFrame 转 records）。
    """
    code = data.normalize_ts_code(ts_code) if ts_code else None
    df = backtest.run(ts_code=code, lookforward_days=lookforward_days)
    return {"ts_code": code, "lookforward_days": lookforward_days, "signals": _df_records(df)}


def main() -> None:
    """stdio 模式启动 MCP server（agent 工具 spawn 本进程后通过 stdin/stdout 通信）。

    ``show_banner=False`` 关掉 fastmcp 启动横幅--横幅会调 ``check_for_newer_version()``
    走 httpx 查 PyPI，而 config 里的 SOCKS 代理会让没装 socksio 的 httpx 崩。
    stdio 协议本身不碰 httpx，关横幅即绕开。
    """
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
