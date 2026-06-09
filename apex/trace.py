"""Analysis trace: structured event stream emitted by analyze.run().

Each analysis produces a list of events (context injection / AI assistant text /
tool call / tool result / verdict). The full list is:
  - emitted live via on_progress(event: dict) for UI streaming
  - persisted to ~/.stock-journal/<ts_code>.trace.jsonl (one JSON line per analysis)

Trace is stored separately from the journal so journal entries stay small and
the trace file can grow freely with raw tool returns.
"""
import json
from pathlib import Path
from typing import Optional

from apex import config


# ── Event helpers ──────────────────────────────────────────────────────────
# Event shape: {"type": <kind>, ...kind-specific fields, "ts": <iso-time>}
# Kinds:
#   context        : {name, content}
#   assistant_text : {iteration, content}
#   tool_call      : {iteration, tool_call_id, name, args}
#   tool_result    : {iteration, tool_call_id, name, summary, raw}
#   verdict_rejected : {iteration, missing}
#   verdict_recorded : {iteration, verdict, confidence}
#   status         : {message}                      (free-form milestone)


def _trace_path(ts_code: str) -> Path:
    return Path(config.get()["paths"]["journal_dir"]).expanduser() / f"{ts_code}.trace.jsonl"


def write_trace(ts_code: str, analyzed_at: str, events: list[dict]) -> None:
    """Append one record to <ts_code>.trace.jsonl. Each record wraps the full event list."""
    path = _trace_path(ts_code)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts_code": ts_code,
        "analyzed_at": analyzed_at,
        "events": events,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_trace(ts_code: str, analyzed_at: str) -> Optional[dict]:
    """Find the trace record matching (ts_code, analyzed_at). Returns None if not found."""
    path = _trace_path(ts_code)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("analyzed_at") == analyzed_at:
                return rec
    return None


# ── Per-tool result summarizers ────────────────────────────────────────────
# Each takes the raw JSON string (or already-parsed value) and returns a short
# dict describing the highlights. Failure-tolerant: any exception → {"error": ...}.


def _safe_parse(raw):
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def summarize_get_daily_price(raw) -> dict:
    bars = _safe_parse(raw)
    if not isinstance(bars, list) or not bars:
        return {"note": "无 K 线数据"}
    last = bars[-1]
    return {
        "bars": len(bars),
        "last_date": str(last.get("trade_date", "")),
        "close": last.get("close"),
        "ma5": last.get("ma5"),
        "ma20": last.get("ma20"),
        "ma60": last.get("ma60"),
        "vol_ratio": last.get("vol_ratio"),
    }


def summarize_get_fundamentals(raw) -> dict:
    data = _safe_parse(raw)
    if isinstance(data, dict) and data.get("error"):
        return {"error": data["error"]}
    if isinstance(data, list) and data:
        row = data[0]
        return {
            "pe": row.get("pe"),
            "pe_ttm": row.get("pe_ttm"),
            "pb": row.get("pb"),
            "ps_ttm": row.get("ps_ttm"),
            "dv_ttm": row.get("dv_ttm"),
            "turnover_rate": row.get("turnover_rate"),
            "circ_mv_yi": round(row["circ_mv"] / 10000, 2) if row.get("circ_mv") else None,
        }
    return {"note": "无基本面数据"}


def summarize_get_stock_info(raw) -> dict:
    data = _safe_parse(raw)
    if isinstance(data, dict) and data.get("error"):
        return {"error": data["error"]}
    if isinstance(data, list) and data:
        row = data[0]
        return {
            "name": row.get("name"),
            "industry": row.get("industry"),
            "list_date": row.get("list_date"),
            "market": row.get("market"),
        }
    return {"note": "无公司信息"}


def summarize_web_search(raw) -> dict:
    data = _safe_parse(raw)
    if not isinstance(data, dict):
        return {"note": "返回不可解析"}
    if data.get("error"):
        return {"error": data["error"]}
    results = data.get("results") or []
    top = []
    for r in results[:3]:
        top.append({
            "title": r.get("title", "")[:60],
            "site": r.get("site", ""),
            "date": r.get("date", ""),
            "url": r.get("url", ""),
        })
    return {
        "category": data.get("category"),
        "query": data.get("query"),
        "freshness": data.get("freshness"),
        "count": data.get("count", len(results)),
        "top3": top,
    }


def summarize_get_dragon_tiger_list(raw) -> dict:
    data = _safe_parse(raw)
    if not isinstance(data, dict):
        return {"note": "返回不可解析"}
    if data.get("error"):
        return {"error": data["error"]}
    return {
        "window_days": data.get("window_days"),
        "list_count": data.get("list_count", 0),
        "recent_dates": (data.get("dates") or [])[:5],
        "note": data.get("note"),
        "has_seats": bool(data.get("seats_recent3")),
    }


def summarize_get_unlock_schedule(raw) -> dict:
    data = _safe_parse(raw)
    if not isinstance(data, dict):
        return {"note": "返回不可解析"}
    if data.get("error"):
        return {"error": data["error"]}
    future = data.get("future_unlocks") or []
    history = data.get("historical_unlocks") or []
    summary = {
        "future_count": len(future),
        "history_count": len(history),
        "note": data.get("note"),
    }
    if future:
        nearest = future[0]
        summary["nearest_unlock"] = {
            "date": nearest.get("unlock_date"),
            "pct_of_circ_mv": nearest.get("pct_of_circ_mv"),
            "market_value_yi": nearest.get("market_value_yi"),
        }
    return summary


def summarize_mx_data_query(raw) -> dict:
    data = _safe_parse(raw)
    if not isinstance(data, dict):
        return {"note": "返回不可解析"}
    if data.get("error"):
        return {"error": data["error"]}
    tables = data.get("tables") or []
    entities = []
    for t in tables:
        rows = t.get("rows") or []
        entities.append({"entity": t.get("entity", ""), "rows": len(rows)})
    return {"source": "mx:data", "query": data.get("query"), "tables": entities}


def summarize_mx_news_search(raw) -> dict:
    data = _safe_parse(raw)
    if not isinstance(data, dict):
        return {"note": "返回不可解析"}
    if data.get("error"):
        return {"error": data["error"]}
    results = data.get("results") or []
    top = []
    for r in results[:5]:
        top.append({"title": r.get("title", "")[:50], "type": r.get("type"), "date": r.get("date")})
    return {"source": "mx:news", "query": data.get("query"), "count": data.get("count"), "top5": top}


def summarize_mx_stock_screen(raw) -> dict:
    data = _safe_parse(raw)
    if not isinstance(data, dict):
        return {"note": "返回不可解析"}
    if data.get("error"):
        return {"error": data["error"]}
    rows = data.get("rows") or []
    return {"source": "mx:screener", "query": data.get("query"), "count": data.get("count"), "sample": rows[:3]}


_SUMMARIZERS = {
    "get_daily_price": summarize_get_daily_price,
    "get_fundamentals": summarize_get_fundamentals,
    "get_stock_info": summarize_get_stock_info,
    "web_search": summarize_web_search,
    "get_dragon_tiger_list": summarize_get_dragon_tiger_list,
    "get_unlock_schedule": summarize_get_unlock_schedule,
    "mx_data_query": summarize_mx_data_query,
    "mx_news_search": summarize_mx_news_search,
    "mx_stock_screen": summarize_mx_stock_screen,
}


def summarize_tool_result(name: str, raw) -> dict:
    fn = _SUMMARIZERS.get(name)
    if fn is None:
        return {"note": f"no summarizer for {name}"}
    try:
        return fn(raw)
    except Exception as e:
        return {"error": f"summarize failed: {type(e).__name__}: {e}"}
