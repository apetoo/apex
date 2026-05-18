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


# 博查搜索类别规范（与 analyze.py 工具 schema 保持一致）
_SEARCH_CATEGORIES: dict[str, dict] = {
    "earnings": {
        "query_tpl": "{name} 业绩 营收 净利润 季报 预告",
        "freshness": "oneMonth",
    },
    "shareholders": {
        "query_tpl": "{name} 减持 增持 大宗交易 解禁",
        "freshness": "oneMonth",
    },
    "regulatory": {
        "query_tpl": "{name} 立案 处罚 诉讼 问询函 警示",
        "freshness": "oneYear",
    },
    "money_flow": {
        "query_tpl": "{name} 北向 龙虎榜 主力 机构",
        "freshness": "oneWeek",
    },
    "corporate_actions": {
        "query_tpl": "{name} 定增 配股 回购 重组 并购",
        "freshness": "oneYear",
    },
    "research": {
        "query_tpl": "{name} 研报 评级 目标价 上调 下调",
        "freshness": "oneMonth",
    },
    "industry": {
        "query_tpl": "{industry} 政策 景气 需求 补贴",
        "freshness": "oneYear",
    },
    "general": {
        "query_tpl": None,  # 用 caller 传入的 query 字段
        "freshness": "oneMonth",
    },
}

MANDATORY_SEARCH_CATEGORIES = ["earnings", "shareholders", "regulatory", "money_flow"]


def web_search(
    ts_code: str,
    category: str = "general",
    name: str = "",
    industry: str = "",
    query: str = "",
    freshness: str = "",
    count: int = 10,
) -> str:
    """Search Bocha with category-based query templates. Returns JSON string.

    category: one of _SEARCH_CATEGORIES keys. Determines query template + default freshness.
    name / industry: substituted into the template.
    query: only used when category == 'general'.
    freshness: optional override of the category's default.
    """
    from apex import config
    api_key = config.get().get("bocha", {}).get("api_key", "")
    if not api_key:
        return json.dumps({"error": "bocha.api_key not configured"})

    cat_spec = _SEARCH_CATEGORIES.get(category)
    if cat_spec is None:
        return json.dumps({
            "error": f"unknown category '{category}'. "
                     f"valid: {list(_SEARCH_CATEGORIES.keys())}"
        })

    code_short = ts_code.split(".")[0]

    if category == "general":
        q = query or (
            f"{name} 公告 研报 新闻" if name
            else f"{code_short} 公告 研报 新闻"
        )
    elif category == "industry":
        if not industry:
            return json.dumps({
                "error": "category=industry 必须传 industry 参数（先调 get_stock_info 拿到行业）"
            })
        q = cat_spec["query_tpl"].format(industry=industry)
    else:
        # 其他分类需要 name；缺失时退化为 ts_code（召回会变差）
        actual_name = name or code_short
        q = cat_spec["query_tpl"].format(name=actual_name)

    actual_freshness = freshness or cat_spec["freshness"]

    payload = json.dumps({
        "query": q,
        "freshness": actual_freshness,
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
        "category": category,
        "query": q,
        "freshness": actual_freshness,
        "count": len(results),
        "results": results,
    }, ensure_ascii=False)


# ── 市场 / 板块 / 资金面 context ────────────────────────────────────────────────
# 这些 helper 不注册成 tool（避免 AI 自己决定要不要查）；
# analyze.run() 每次都自动注入到 user prompt。

_INDEX_NAMES: dict[str, str] = {
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "899050.BJ": "北证50",
}


def _select_indices(ts_code: str) -> list[str]:
    """按板块挑要展示的指数。沪深300 永远包含（A 股核心 benchmark）。"""
    base = ["000300.SH"]
    if ts_code.endswith(".SH"):
        symbol = ts_code.split(".")[0]
        if symbol.startswith("688"):
            return base + ["000001.SH", "000688.SH"]
        return base + ["000001.SH"]
    if ts_code.endswith(".SZ"):
        symbol = ts_code.split(".")[0]
        if symbol.startswith("3"):
            return base + ["399001.SZ", "399006.SZ"]
        return base + ["399001.SZ"]
    if ts_code.endswith(".BJ"):
        return base + ["899050.BJ"]
    return base


def _summarize_bars(code: str, bars: list[dict]) -> Optional[dict]:
    """从日线序列里算关键统计：当日/5日/20日涨跌、60日位置、量比。bars 按 trade_date 升序。"""
    if not bars or len(bars) < 2:
        return None
    latest = bars[-1]
    prev = bars[-2]
    try:
        close_now = float(latest["close"])
        close_prev = float(prev["close"])
    except (KeyError, TypeError, ValueError):
        return None
    if close_prev <= 0:
        return None
    daily_chg = (close_now - close_prev) / close_prev * 100

    def _n_day_chg(n: int) -> Optional[float]:
        if len(bars) < n + 1:
            return None
        try:
            base = float(bars[-(n + 1)]["close"])
        except (TypeError, ValueError):
            return None
        if base <= 0:
            return None
        return (close_now - base) / base * 100

    chg_5 = _n_day_chg(5)
    chg_20 = _n_day_chg(20)

    # 60 日区间位置
    position = None
    window = bars[-min(60, len(bars)):]
    if len(window) >= 10:
        try:
            highs = [float(b["close"]) for b in window]
            hi, lo = max(highs), min(highs)
            position = ((close_now - lo) / (hi - lo) * 100) if hi > lo else 50.0
        except (TypeError, ValueError):
            position = None

    # 量比（今日 vs 前 5 日均量，若有 vol 字段）
    vol_ratio = None
    try:
        vol_today = float(latest.get("vol") or 0)
        vols = [float(b.get("vol") or 0) for b in bars[-6:-1]]
        avg = sum(vols) / len(vols) if vols else 0
        if avg > 0 and vol_today > 0:
            vol_ratio = vol_today / avg
    except (TypeError, ValueError):
        pass

    return {
        "code": code,
        "name": _INDEX_NAMES.get(code, code),
        "close": round(close_now, 2),
        "trade_date": str(latest.get("trade_date", "")),
        "daily_chg_pct": round(daily_chg, 2),
        "chg_5d_pct": round(chg_5, 2) if chg_5 is not None else None,
        "chg_20d_pct": round(chg_20, 2) if chg_20 is not None else None,
        "position_60d_pct": round(position, 0) if position is not None else None,
        "vol_ratio_5d": round(vol_ratio, 2) if vol_ratio else None,
    }


def _fetch_index_bars(codes: list[str], days: int = 30) -> dict[str, list[dict]]:
    """批量取指数日线，返回 {code: bars_asc}。失败的 code 对应空 list。"""
    result: dict[str, list[dict]] = {c: [] for c in codes}
    if not codes:
        return result
    try:
        pro = _tushare()
    except Exception:
        return result
    end = datetime.today().strftime("%Y%m%d")
    start = (datetime.today() - timedelta(days=days + 30)).strftime("%Y%m%d")
    for code in codes:
        try:
            df = pro.index_daily(
                ts_code=code, start_date=start, end_date=end,
                fields="trade_date,close,vol,pct_chg",
            )
            if df is None or df.empty:
                continue
            df = df.sort_values("trade_date").reset_index(drop=True)
            result[code] = df.tail(days).to_dict(orient="records")
        except Exception:
            continue
    return result


def _get_sw_l1_for_stock(ts_code: str) -> Optional[str]:
    """查股票所属 申万 L1 行业指数代码。失败返 None（如基础账户没权限）。"""
    try:
        pro = _tushare()
        df = pro.index_member(ts_code=ts_code)
        if df is None or df.empty:
            return None
        for _, row in df.iterrows():
            idx = str(row.get("index_code") or "")
            # 申万 L1 代码格式：801XXX.SI，三位末段
            if idx.startswith("801") and idx.endswith(".SI") and len(idx) == 9:
                return idx
        return None
    except Exception:
        return None


def _fetch_sector_summary(sw_code: str, days: int = 25) -> Optional[dict]:
    """申万 L1 行业指数最近 N 天 summary。"""
    try:
        pro = _tushare()
        end = datetime.today().strftime("%Y%m%d")
        start = (datetime.today() - timedelta(days=days + 30)).strftime("%Y%m%d")
        df = pro.sw_daily(
            ts_code=sw_code, start_date=start, end_date=end,
            fields="trade_date,close,vol,pct_change",
        )
        if df is None or df.empty:
            return None
        # sw_daily 用 pct_change，重命名以复用 _summarize_bars
        df = df.rename(columns={"pct_change": "pct_chg"})
        df = df.sort_values("trade_date").reset_index(drop=True)
        bars = df.tail(days).to_dict(orient="records")
        return _summarize_bars(sw_code, bars)
    except Exception:
        return None


def _fetch_north_money(days: int = 6) -> Optional[dict]:
    """北向资金近 N 个交易日净买入。单位：百万元 → 折算为亿元。"""
    try:
        pro = _tushare()
        end = datetime.today().strftime("%Y%m%d")
        start = (datetime.today() - timedelta(days=days + 10)).strftime("%Y%m%d")
        df = pro.moneyflow_hsgt(start_date=start, end_date=end)
        if df is None or df.empty:
            return None
        df = df.sort_values("trade_date").reset_index(drop=True)
        recent = df.tail(days)
        if "north_money" not in recent.columns:
            return None
        # 单位：百万元 → 亿元（÷100）
        north = recent["north_money"].astype(float).tolist()
        today = north[-1] if north else None
        return {
            "today_yi": round(today / 100, 2) if today is not None else None,
            "cumulative_5d_yi": round(sum(north[-5:]) / 100, 2) if north else None,
            "trade_dates": [str(d) for d in recent["trade_date"].tolist()],
        }
    except Exception:
        return None


def _compute_stock_relative(
    ts_code: str,
    indices: list[dict],
    sector: Optional[dict],
) -> Optional[dict]:
    """个股 5/20 日涨跌 + 相对大盘/板块的强弱差。"""
    try:
        pro = _tushare()
        end = datetime.today().strftime("%Y%m%d")
        start = (datetime.today() - timedelta(days=40)).strftime("%Y%m%d")
        df = pro.daily(
            ts_code=ts_code, start_date=start, end_date=end,
            fields="trade_date,close",
        )
        if df is None or df.empty or len(df) < 2:
            return None
        df = df.sort_values("trade_date").reset_index(drop=True)
        close = df["close"].astype(float).tolist()
        chg_5d = ((close[-1] - close[-6]) / close[-6] * 100) if len(close) >= 6 and close[-6] > 0 else None
        chg_20d = ((close[-1] - close[-21]) / close[-21] * 100) if len(close) >= 21 and close[-21] > 0 else None

        # benchmark：优先沪深300
        ref = next((i for i in indices if i["code"] == "000300.SH"), None)
        if ref is None and indices:
            ref = indices[0]

        result = {
            "chg_5d_pct": round(chg_5d, 2) if chg_5d is not None else None,
            "chg_20d_pct": round(chg_20d, 2) if chg_20d is not None else None,
        }
        if ref and chg_5d is not None and ref.get("chg_5d_pct") is not None:
            result["vs_index_5d_pct"] = round(chg_5d - ref["chg_5d_pct"], 2)
            result["ref_index_name"] = ref["name"]
        if sector and chg_5d is not None and sector.get("chg_5d_pct") is not None:
            result["vs_sector_5d_pct"] = round(chg_5d - sector["chg_5d_pct"], 2)
            result["sector_name"] = sector["name"]
        return result
    except Exception:
        return None


def get_market_context(ts_code: str) -> str:
    """大盘 + 板块 + 资金面 + 个股相对 综合 context。

    返回 JSON 字符串。每个 section 独立 fail-soft，单项失败不影响其他。
    设计上仅给 analyze.py 调用、不注册为 AI tool（避免 AI 漏调或乱调）。
    """
    result: dict = {
        "as_of": datetime.today().strftime("%Y-%m-%d"),
        "indices": [],
        "sector": None,
        "north_money": None,
        "stock_relative": None,
    }

    # 1. 大盘指数
    try:
        codes = _select_indices(ts_code)
        bars_map = _fetch_index_bars(codes, days=30)
        for code in codes:
            s = _summarize_bars(code, bars_map.get(code) or [])
            if s:
                result["indices"].append(s)
        if result["indices"]:
            result["as_of"] = result["indices"][0]["trade_date"] or result["as_of"]
    except Exception:
        pass

    # 2. 板块（申万 L1）
    try:
        sw_code = _get_sw_l1_for_stock(ts_code)
        if sw_code:
            result["sector"] = _fetch_sector_summary(sw_code)
    except Exception:
        pass

    # 3. 北向资金
    try:
        result["north_money"] = _fetch_north_money()
    except Exception:
        pass

    # 4. 个股相对强度
    try:
        result["stock_relative"] = _compute_stock_relative(
            ts_code, result["indices"], result["sector"],
        )
    except Exception:
        pass

    return json.dumps(result, ensure_ascii=False)


def get_dragon_tiger_list(ts_code: str, days: int = 90, fetch_seats: bool = False) -> str:
    """近 N 天该股龙虎榜上榜情况。返回 JSON：上榜日期列表 + 上榜频次。

    days: 回溯天数（自然日，默认 90）。
    fetch_seats: 是否对每个上榜日再拉买卖席位 TOP5（默认 False，避免 API 风暴）。
        True 时只对最近 3 个上榜日拉席位，防止 token 爆炸。

    依赖 akshare 东财接口（无 token）。失败时返回 {"error": ...}，不抛异常。
    """
    try:
        import akshare as ak
    except ImportError:
        return json.dumps({"error": "akshare not installed"})

    symbol = ts_code.split(".")[0]
    cutoff = datetime.today().date() - timedelta(days=days)

    try:
        df_dates = ak.stock_lhb_stock_detail_date_em(symbol=symbol)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})

    if df_dates is None or df_dates.empty:
        return json.dumps({
            "ts_code": ts_code,
            "window_days": days,
            "list_count": 0,
            "dates": [],
            "note": "无龙虎榜上榜记录",
        }, ensure_ascii=False)

    df_dates["交易日"] = pd.to_datetime(df_dates["交易日"], errors="coerce").dt.date
    recent = df_dates[df_dates["交易日"] >= cutoff].sort_values("交易日", ascending=False)
    dates = [d.isoformat() for d in recent["交易日"].tolist() if d is not None]

    result: dict = {
        "ts_code": ts_code,
        "window_days": days,
        "list_count": len(dates),
        "dates": dates,
    }

    if fetch_seats and dates:
        seats_by_date: list[dict] = []
        for date_str in dates[:3]:  # 最近 3 次足够，避免 N 次 HTTP
            date_compact = date_str.replace("-", "")
            day_record: dict = {"date": date_str, "buy_top": [], "sell_top": []}
            for flag, key in [("买入", "buy_top"), ("卖出", "sell_top")]:
                try:
                    df_s = ak.stock_lhb_stock_detail_em(
                        symbol=symbol, date=date_compact, flag=flag,
                    )
                    if df_s is None or df_s.empty:
                        continue
                    top5 = df_s.head(5)
                    for _, row in top5.iterrows():
                        seat = (row.get("交易营业部名称") or "").strip()
                        amount = row.get("买入金额") if flag == "买入" else row.get("卖出金额")
                        net = row.get("净额")
                        day_record[key].append({
                            "seat": seat,
                            "amount_yi": round(float(amount) / 1e8, 3) if pd.notna(amount) else None,
                            "net_yi": round(float(net) / 1e8, 3) if pd.notna(net) else None,
                        })
                except Exception:
                    continue
            seats_by_date.append(day_record)
        result["seats_recent3"] = seats_by_date

    return json.dumps(result, ensure_ascii=False, default=str)


def get_unlock_schedule(ts_code: str, days_ahead: int = 180,
                        history_days: int = 180) -> str:
    """限售解禁日程。返回 JSON：未来 N 天待解禁 + 近 N 天已发生解禁（含解禁后 20 日表现）。

    days_ahead: 未来回看（默认 180 天，覆盖半年内的解禁风险）。
    history_days: 历史回看（默认 180 天，用于评估"该股以往解禁后股价反应"）。

    依赖 akshare 东财接口（无 token）。失败时返回 {"error": ...}，不抛异常。
    """
    try:
        import akshare as ak
    except ImportError:
        return json.dumps({"error": "akshare not installed"})

    symbol = ts_code.split(".")[0]
    today = datetime.today().date()
    future_cutoff = today + timedelta(days=days_ahead)
    history_cutoff = today - timedelta(days=history_days)

    try:
        df = ak.stock_restricted_release_queue_em(symbol=symbol)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})

    if df is None or df.empty:
        return json.dumps({
            "ts_code": ts_code,
            "future_unlocks": [],
            "historical_unlocks": [],
            "note": "无解禁数据（可能是次新股或全流通）",
        }, ensure_ascii=False)

    df["解禁时间"] = pd.to_datetime(df["解禁时间"], errors="coerce").dt.date

    def _row_to_dict(row, include_perf: bool) -> dict:
        circ_ratio = row.get("占流通市值比例")
        market_cap = row.get("实际解禁数量市值")
        d: dict = {
            "unlock_date": row["解禁时间"].isoformat() if row["解禁时间"] else None,
            "share_type": row.get("限售股类型"),
            "shares_10k": round(float(row.get("实际解禁数量") or 0) / 1e4, 2),
            "market_value_yi": round(float(market_cap) / 1e8, 3) if pd.notna(market_cap) else None,
            "pct_of_circ_mv": round(float(circ_ratio) * 100, 3) if pd.notna(circ_ratio) else None,
        }
        if include_perf:
            pre20 = row.get("解禁前20日涨跌幅")
            post20 = row.get("解禁后20日涨跌幅")
            d["pre_20d_pct"] = round(float(pre20) * 100, 2) if pd.notna(pre20) else None
            d["post_20d_pct"] = round(float(post20) * 100, 2) if pd.notna(post20) else None
        return d

    future_rows = df[(df["解禁时间"].notna())
                     & (df["解禁时间"] >= today)
                     & (df["解禁时间"] <= future_cutoff)]
    history_rows = df[(df["解禁时间"].notna())
                      & (df["解禁时间"] < today)
                      & (df["解禁时间"] >= history_cutoff)]

    future = [_row_to_dict(row, include_perf=False)
              for _, row in future_rows.sort_values("解禁时间").iterrows()]
    history = [_row_to_dict(row, include_perf=True)
               for _, row in history_rows.sort_values("解禁时间", ascending=False).iterrows()]

    return json.dumps({
        "ts_code": ts_code,
        "as_of": today.isoformat(),
        "future_unlocks": future,
        "historical_unlocks": history,
    }, ensure_ascii=False, default=str)


# Tool dispatch map used by analyze.py
TOOL_FUNCTIONS = {
    "get_daily_price": get_daily_price,
    "get_fundamentals": get_fundamentals,
    "get_stock_info": get_stock_info,
    "web_search": web_search,
    "get_dragon_tiger_list": get_dragon_tiger_list,
    "get_unlock_schedule": get_unlock_schedule,
}
