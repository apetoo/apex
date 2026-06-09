"""
妙想 MX API client — unified wrapper for 3 endpoints:
  query         金融数据自然语言查询
  news-search   财经资讯搜索
  stock-screen  智能选股

All use MX_APIKEY (config → env var). Returns JSON strings for the AI agent.
"""
import json
import os
import urllib.error
import urllib.request
from typing import Any

_BASE = "https://mkapi2.dfcfs.com/finskillshub/api/claw"


def _api_key() -> str:
    from apex import config as _cfg
    return ((_cfg.get() or {}).get("mx") or {}).get("api_key", "") or os.environ.get("MX_APIKEY", "")


def _post(url: str, payload: dict, key: str) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "apikey": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body[:500]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _check_key() -> str:
    k = _api_key()
    if not k:
        raise ValueError("MX_APIKEY not configured (config.yaml → mx.api_key or env var MX_APIKEY)")
    return k


def _check_status(result: dict) -> str | None:
    status = result.get("status")
    if status and status != 0:
        return f"API error {status}: {result.get('message', '')}"
    if result.get("error"):
        return result["error"]
    return None


# ── Public tool functions ────────────────────────────────────────────────────


def mx_data_query(query_text: str) -> str:
    """自然语言查询金融数据（行情/财务/股东/板块/指数等）。

    Args:
        query_text: 自然语言问句，如 "贵州茅台近三年净利润 营业收入"
    """
    try:
        key = _check_key()
    except ValueError as e:
        return json.dumps({"error": str(e)})

    result = _post(f"{_BASE}/query", {"toolQuery": query_text}, key)
    err = _check_status(result)
    if err:
        return json.dumps({"source": "mx:data", "query": query_text, "error": err})

    try:
        dto_list = result["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"]
    except (KeyError, TypeError):
        return json.dumps({"source": "mx:data", "query": query_text,
                           "error": "unexpected response structure",
                           "raw": str(result)[:800]})

    tables = []
    for dto in dto_list if isinstance(dto_list, list) else []:
        entity = dto.get("entityName") or ""
        title = dto.get("title") or ""
        name_map = dto.get("nameMap") or {}
        table = dto.get("table") or {}
        headers = table.get("headName") or []

        if isinstance(name_map, list):
            name_map = {str(i): v for i, v in enumerate(name_map)}
        elif not isinstance(name_map, dict):
            name_map = {}
        col_map = {str(k): str(v) for k, v in name_map.items()}

        indicator_keys = [k for k in table.keys() if k != "headName"]
        rows = []
        for row_idx, date in enumerate(headers):
            row = {"date": str(date)}
            for key in indicator_keys:
                label = col_map.get(str(key), str(key))
                vals = table.get(key, [])
                row[label] = str(vals[row_idx]) if isinstance(vals, list) and row_idx < len(vals) else ""
            rows.append(row)

        tables.append({"entity": entity, "title": title,
                       "rows": rows[:60], "total_rows": len(rows)})

    return json.dumps({"source": "mx:data", "query": query_text, "tables": tables},
                      ensure_ascii=False)


def mx_news_search(query: str) -> str:
    """财经资讯搜索（新闻/研报/公告）。

    Args:
        query: 搜索问句，如 "格力电器最新研报" "贵州茅台利空"
    """
    try:
        key = _check_key()
    except ValueError as e:
        return json.dumps({"error": str(e)})

    result = _post(f"{_BASE}/news-search", {"query": query}, key)
    err = _check_status(result)
    if err:
        return json.dumps({"source": "mx:news", "query": query, "error": err})

    try:
        items = result["data"]["data"]["llmSearchResponse"]["data"]
    except (KeyError, TypeError):
        return json.dumps({"source": "mx:news", "query": query,
                           "error": "unexpected response",
                           "raw": str(result)[:500]})

    type_cn = {"REPORT": "研报", "NEWS": "新闻", "ANNOUNCEMENT": "公告"}
    news = []
    for item in (items or []):
        news.append({
            "title": item.get("title", ""),
            "content": (item.get("content") or "")[:600],
            "date": (item.get("date") or "").split()[0] if item.get("date") else "",
            "type": type_cn.get(item.get("informationType", ""), ""),
            "institution": item.get("insName", ""),
            "rating": item.get("rating", ""),
            "entity": item.get("entityFullName", ""),
        })

    return json.dumps({"source": "mx:news", "query": query, "count": len(news),
                       "results": news}, ensure_ascii=False)


def mx_stock_screen(query: str) -> str:
    """自然语言智能选股。

    Args:
        query: 选股条件，如 "市盈率低于20且ROE大于15%的A股"
    """
    try:
        key = _check_key()
    except ValueError as e:
        return json.dumps({"error": str(e)})

    result = _post(f"{_BASE}/stock-screen", {"keyword": query}, key)
    err = _check_status(result)
    if err:
        return json.dumps({"source": "mx:screener", "query": query, "error": err})

    rows: list[dict[str, str]] = []
    try:
        inner = result["data"]["data"]
        data_list = inner.get("allResults", {}).get("result", {}).get("dataList", [])
        columns = inner.get("allResults", {}).get("result", {}).get("columns", [])
    except (KeyError, TypeError):
        data_list, columns = [], []

    if data_list:
        col_map = {}
        for col in columns or []:
            if isinstance(col, dict):
                en = (col.get("field") or col.get("name") or col.get("key") or "")
                cn = (col.get("displayName") or col.get("title") or col.get("label") or en)
                col_map[str(en)] = str(cn)

        for row in data_list[:30] if isinstance(data_list, list) else []:
            if not isinstance(row, dict):
                continue
            cn_row = {}
            for k, v in row.items():
                label = col_map.get(str(k), str(k))
                if v is None:
                    cn_row[label] = ""
                elif isinstance(v, (dict, list)):
                    cn_row[label] = json.dumps(v, ensure_ascii=False)
                else:
                    cn_row[label] = str(v)
            rows.append(cn_row)
    else:
        # Fallback: parse partialResults markdown table
        try:
            partial = inner.get("partialResults", "")
        except Exception:
            partial = ""
        if isinstance(partial, str) and partial.strip():
            lines = [l.strip() for l in partial.split("\n") if l.strip()]
            if len(lines) > 0:
                header = [c.strip() for c in lines[0].split("|") if c.strip()]
                data_start = 2 if len(lines) > 1 and "--" in lines[1] else 1
                for line in lines[data_start:]:
                    cells = [c.strip() for c in line.split("|") if c.strip()]
                    if len(cells) == len(header):
                        rows.append(dict(zip(header, cells)))

    return json.dumps({"source": "mx:screener", "query": query, "count": len(rows),
                       "rows": rows}, ensure_ascii=False)
