"""
Data layer: tushare + akshare + bocha. Functions here are also registered as tools
for the Claude API agent in analyze.py.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

_BOCHA_API_URL = "https://api.bochaai.com/v1/web-search"


def normalize_ts_code(code: str) -> str:
    """Add exchange suffix if missing. 6xxxxx → .SH, 0/3xxxxx → .SZ, 8/4xxxxx → .BJ."""
    code = (code or "").strip().upper()
    if not code or "." in code:
        return code
    if not code.isdigit() or len(code) != 6:
        return code
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("8", "4")):
        return f"{code}.BJ"
    return code

_ts_api = None


def _tushare():
    global _ts_api
    if _ts_api is None:
        import tushare as ts
        from apex import config
        cfg = config.get()
        ts.set_token(cfg["tushare"]["token"])
        _ts_api = ts.pro_api()
    return _ts_api


def get_daily_price(ts_code: str, start_date: str = None, end_date: str = None,
                    adj: str = "qfq") -> str:
    """Return daily OHLCV + MA data as JSON string. start/end: YYYYMMDD.

    adj: 'qfq'（前复权，默认；MA / 趋势分析必须用此）/ 'hfq'（后复权）/ 'none'（不复权，
    真实历史成交价，仅用于回测建模实际成交成本）。
    """
    if not start_date:
        start_date = (datetime.today() - timedelta(days=120)).strftime("%Y%m%d")
    if not end_date:
        end_date = datetime.today().strftime("%Y%m%d")

    # Normalize YYYY-MM-DD → YYYYMMDD
    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")

    try:
        _tushare()  # 确保 token 已 set
        import tushare as ts_module
        adj_param = None if adj == "none" else adj
        df = ts_module.pro_bar(ts_code=ts_code, adj=adj_param, freq="D",
                                start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            raise ValueError("empty")
        df = df.sort_values("trade_date").reset_index(drop=True)
    except Exception:
        # Fallback: akshare（同样使用复权口径，保持与 tushare 一致）
        import akshare as ak
        symbol = ts_code.split(".")[0]
        ak_adjust = "" if adj == "none" else adj
        df = ak.stock_zh_a_hist(symbol=symbol, start_date=start_date,
                                 end_date=end_date, adjust=ak_adjust)
        df = df.rename(columns={"日期": "trade_date", "开盘": "open", "收盘": "close",
                                  "最高": "high", "最低": "low", "成交量": "vol"})
        df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")

    # Add basic MAs
    df["ma5"] = df["close"].rolling(5).mean().round(2)
    df["ma10"] = df["close"].rolling(10).mean().round(2)
    df["ma20"] = df["close"].rolling(20).mean().round(2)
    df["ma60"] = df["close"].rolling(60).mean().round(2)

    # Volume ratio (today vol / 5-day avg vol)
    df["vol_ratio"] = (df["vol"] / df["vol"].rolling(5).mean()).round(2)

    result = df[["trade_date", "open", "high", "low", "close", "vol",
                  "ma5", "ma10", "ma20", "ma60", "vol_ratio"]].tail(60)
    return result.to_json(orient="records", force_ascii=False)


def get_fundamentals(ts_code: str) -> str:
    """Return latest PE/PB/turnover_rate/circ_mv as JSON string."""
    try:
        pro = _tushare()
        today = datetime.today().strftime("%Y%m%d")
        df = pro.daily_basic(ts_code=ts_code, trade_date=today,
                              fields="ts_code,trade_date,pe,pe_ttm,pb,ps_ttm,dv_ttm,turnover_rate,circ_mv")
        if df is None or df.empty:
            # Try previous trading day
            prev = (datetime.today() - timedelta(days=3)).strftime("%Y%m%d")
            df = pro.daily_basic(ts_code=ts_code, start_date=prev, end_date=today,
                                  fields="ts_code,trade_date,pe,pe_ttm,pb,ps_ttm,dv_ttm,turnover_rate,circ_mv")
        if df is None or df.empty:
            return json.dumps({"error": "no fundamental data"})
        return df.tail(1).to_json(orient="records", force_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def get_stock_info(ts_code: str) -> str:
    """Return company name, industry, list_date."""
    try:
        pro = _tushare()
        df = pro.stock_basic(ts_code=ts_code, fields="ts_code,name,industry,list_date,market")
        if df is None or df.empty:
            return json.dumps({"error": "not found"})
        return df.to_json(orient="records", force_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def get_realtime_price(ts_codes: list[str]) -> dict[str, Optional[float]]:
    """Batch fetch intraday real-time price from Sina Finance (one HTTP call, bypasses proxy)."""
    import re
    result: dict[str, Optional[float]] = {c: None for c in ts_codes}
    if not ts_codes:
        return result

    sina_codes = []
    for c in ts_codes:
        symbol = c.split(".")[0]
        if c.endswith(".SH"):
            sina_codes.append(f"sh{symbol}")
        elif c.endswith(".SZ"):
            sina_codes.append(f"sz{symbol}")
        elif c.endswith(".BJ"):
            sina_codes.append(f"bj{symbol}")
    if not sina_codes:
        return result

    url = f"https://hq.sinajs.cn/list={','.join(sina_codes)}"
    try:
        req = urllib.request.Request(url, headers={
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0",
        })
        # 显式跳过系统代理（东财/新浪在国内，外代理会断连）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
    except Exception:
        return result

    symbol_to_ts = {c.split(".")[0]: c for c in ts_codes}
    pattern = re.compile(r'var hq_str_(sh|sz|bj)(\d{6})="([^"]*)";')
    for line in raw.split("\n"):
        m = pattern.match(line.strip())
        if not m:
            continue
        symbol = m.group(2)
        fields = m.group(3).split(",")
        # Sina format: name, open, prev_close, current, high, low, ...
        if len(fields) < 4:
            continue
        try:
            price = float(fields[3])
        except (ValueError, IndexError):
            continue
        if price > 0 and symbol in symbol_to_ts:
            result[symbol_to_ts[symbol]] = price
    return result


def get_latest_price(ts_codes: list[str]) -> dict[str, Optional[float]]:
    """Batch fetch latest close price for multiple stocks. Returns {ts_code: price}."""
    result: dict[str, Optional[float]] = {c: None for c in ts_codes}
    try:
        pro = _tushare()
        today = datetime.today().strftime("%Y%m%d")
        prev3 = (datetime.today() - timedelta(days=5)).strftime("%Y%m%d")
        codes_str = ",".join(ts_codes)
        df = pro.daily(ts_code=codes_str, start_date=prev3, end_date=today,
                        fields="ts_code,trade_date,close")
        if df is not None and not df.empty:
            latest = df.sort_values("trade_date").groupby("ts_code").last()
            for code in ts_codes:
                if code in latest.index:
                    result[code] = float(latest.loc[code, "close"])
    except Exception:
        pass
    return result


# 博查结果信任度分级：数字越小越高信任
_SITE_TRUST: dict[str, int] = {
    # 官方公告源
    "cninfo.com.cn": 1, "sse.com.cn": 1, "szse.cn": 1, "bse.cn": 1,
    # 主流财经平台
    "eastmoney.com": 2, "10jqka.com.cn": 2, "xueqiu.com": 2,
    "sina.com.cn": 3, "qq.com": 3, "163.com": 3,
}

def _site_trust(site: str) -> int:
    site = (site or "").lower()
    for k, v in _SITE_TRUST.items():
        if k in site:
            return v
    return 9  # 未知来源最低优先级


def web_search(ts_code: str, name: str = "", query: str = "", freshness: str = "oneMonth", count: int = 10) -> str:
    """Search Bocha for stock news, announcements, and research reports. Returns JSON string."""
    from apex import config
    api_key = config.get().get("bocha", {}).get("api_key", "")
    if not api_key:
        return json.dumps({"error": "bocha.api_key not configured"})

    code_short = ts_code.split(".")[0]
    q = query or (
        f"{name} 公告 研报 新闻" if name
        else f"{code_short} 公告 研报 新闻"
    )
    payload = json.dumps({
        "query": q,
        "freshness": freshness,
        "summary": False,
        "count": count,
    }).encode("utf-8")
    req = urllib.request.Request(
        _BOCHA_API_URL,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        return json.dumps({"error": f"HTTP {e.code}: {body[:300]}"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})

    if raw.get("code") != 200:
        return json.dumps({"error": raw.get("msg") or f"code {raw.get('code')}"})

    items = (raw.get("data") or {}).get("webPages", {}).get("value") or []
    results = []
    for it in items:
        pub_date = it.get("datePublished") or it.get("dateLastCrawled") or ""
        site = (it.get("siteName") or "").strip()
        results.append({
            "title": (it.get("name") or "").strip(),
            "url": it.get("url") or "",
            "snippet": (it.get("snippet") or "").strip().replace("\n", " "),
            "date": pub_date[:10] if pub_date else "",
            "site": site,
            "trust_level": _site_trust(site),
        })
    # 高信任来源排前面，同等信任度按发布日期降序
    results.sort(key=lambda r: (r["trust_level"], -(int((r["date"] or "").replace("-", "") or 0))))
    # 去掉排序辅助字段后返回
    for r in results:
        r.pop("trust_level", None)
    return json.dumps({
        "source": "bocha:web-search",
        "query": q,
        "freshness": freshness,
        "count": len(results),
        "results": results,
    }, ensure_ascii=False)


# Tool dispatch map used by analyze.py
TOOL_FUNCTIONS = {
    "get_daily_price": get_daily_price,
    "get_fundamentals": get_fundamentals,
    "get_stock_info": get_stock_info,
    "web_search": web_search,
}
