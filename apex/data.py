"""
Data layer: tushare + akshare + bocha. Functions here are also registered as tools
for the Claude API agent in analyze.py.
"""
import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests

from apex import mx_client as _mx
from apex.per_day_cache import per_day_cache

logger = logging.getLogger(__name__)

_BOCHA_API_URL = "https://api.bochaai.com/v1/web-search"

# akshare 的 stock_zh_a_minute 内部用 py_mini_racer(V8) 执行新浪解密 JS, V8 的
# AddressPoolManager 是进程级单例, 多线程并发 MiniRacer() 会 FATAL 崩进程。
# FastAPI 同步端点在 threadpool 并发跑(分时看板多股), 必须用此锁序列化 akshare 调用。
_AKSHARE_LOCK = threading.Lock()

# 国内 API 直连 opener: 跳系统代理 + certifi CA(免 macOS Python.framework 系统证书
# 缺失导致 SSL CERTIFICATE_VERIFY_FAILED, 静默 except 让搜索/实时链全失效)。
# 新浪行情 / 博查搜索 / 妙想搜索 的 urllib 调用都走这个。
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


def _direct_opener() -> urllib.request.OpenerDirector:
    """build_opener 跳代理 + certifi SSL context。国内 API urllib 调用走这个。"""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_SSL_CTX),
    )


def normalize_ts_code(code: str) -> str:
    """Add exchange suffix if missing. 6/5xxxxx → .SH, 0/3/1xxxxx → .SZ, 8/4xxxxx → .BJ（旧码）, 9xxxxx → .BJ（920 新码段）。"""
    code = (code or "").strip().upper()
    if not code or "." in code:
        return code
    if not code.isdigit() or len(code) != 6:
        return code
    if code.startswith(("6", "5")):  # 6 沪市个股, 5 沪市 ETF/基金
        return f"{code}.SH"
    if code.startswith(("0", "3", "1")):  # 0/3 深市个股, 1 深市 ETF/基金
        return f"{code}.SZ"
    if code.startswith(("8", "4", "9")):  # 8/4 北交所旧码; 9 = 920xxx 新码段（2025-05 迁移）
        return f"{code}.BJ"
    return code


def is_etf(ts_code: str) -> bool:
    """ETF/基金前缀判断：5xxxxx.SH、1xxxxx.SZ。接受带后缀或裸码。"""
    code = normalize_ts_code(ts_code)
    return code.startswith("5") and code.endswith(".SH") or \
           code.startswith("1") and code.endswith(".SZ")


# 东财 kline 周期 → klt 参数
_EM_KLT = {"30": 30, "60": 60, "D": 101, "W": 102}
_EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
_EM_KLINE_CACHE_TTL = 60.0
_EM_KLINE_CACHE: dict[tuple[str, str, int], tuple[float, list[dict]]] = {}
_EM_SESSION: Optional[requests.Session] = None
_SINA_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/"
    "CN_MarketDataService.getKLineData"
)
_SINA_SESSION: Optional[requests.Session] = None


def _eastmoney_session() -> requests.Session:
    """东财专用直连 Session；不继承本机代理环境变量。"""
    global _EM_SESSION
    if _EM_SESSION is None:
        session = requests.Session()
        session.trust_env = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        })
        _EM_SESSION = session
    return _EM_SESSION


def _sina_session() -> requests.Session:
    """新浪行情专用直连 Session；作为分钟 K 线备用源。"""
    global _SINA_SESSION
    if _SINA_SESSION is None:
        session = requests.Session()
        session.trust_env = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
            "Accept": "application/json,text/plain,*/*",
        })
        _SINA_SESSION = session
    return _SINA_SESSION


def eastmoney_kline(ts_code: str, freq: str = "D", n: int = 250) -> Optional[list[dict]]:
    """东财 push2his K 线（不复权）。缠论页分钟/备用日线源。

    返回 list[dict] 升序：{dt: datetime, open, high, low, close, vol, amount(元)}。
    失败/无数据返回 None。

    注意：push2his 高频会封 IP（RemoteDisconnected，项目前科）——只允许页面级
    低频单次调用，禁止循环批量。调用方自行保证串行。
    """
    code = normalize_ts_code(ts_code)
    klt = _EM_KLT.get(freq)
    if klt is None or "." not in code:
        return None
    cache_key = (code, freq, n)
    now = time.monotonic()
    if freq in ("30", "60"):
        cached = _EM_KLINE_CACHE.get(cache_key)
        if cached and now - cached[0] < _EM_KLINE_CACHE_TTL:
            return [dict(bar) for bar in cached[1]]
    market = "1" if code.endswith(".SH") else "0"  # SH→1, SZ/BJ→0
    secid = f"{market}.{code.split('.')[0]}"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": _EM_UT,
        "klt": klt,
        "fqt": 0,
        "beg": 0,
        "end": 20500101,
        "lmt": n,
    }
    session = _eastmoney_session()
    for attempt in range(1, 3):
        try:
            response = session.get(_EM_KLINE_URL, params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
            break
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 2:
                log = logger.info if freq in ("30", "60") else logger.warning
                log(
                    "eastmoney kline failed code=%s freq=%s attempt=%s error=%s",
                    code, freq, attempt, type(exc).__name__,
                )
                return None
        except (requests.HTTPError, requests.JSONDecodeError, ValueError) as exc:
            log = logger.info if freq in ("30", "60") else logger.warning
            log(
                "eastmoney kline rejected code=%s freq=%s attempt=%s error=%s",
                code, freq, attempt, type(exc).__name__,
            )
            return None
    klines = (payload.get("data") or {}).get("klines")
    if not klines:
        return None
    bars = []
    for line in klines:
        p = line.split(",")
        if len(p) < 7:
            continue
        try:
            dt = datetime.strptime(p[0], "%Y-%m-%d %H:%M") if " " in p[0] \
                else datetime.strptime(p[0], "%Y-%m-%d")
            bars.append({
                "dt": dt, "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "vol": float(p[5]), "amount": float(p[6]),
            })
        except (ValueError, TypeError):
            continue
    if not bars:
        return None
    if freq in ("30", "60"):
        _EM_KLINE_CACHE[cache_key] = (now, [dict(bar) for bar in bars])
    return bars


def sina_minute_kline(ts_code: str, freq: str, n: int = 250) -> Optional[list[dict]]:
    """新浪 30/60 分钟 K 线；东财连接被风控时的备用源。"""
    code = normalize_ts_code(ts_code)
    if freq not in ("30", "60"):
        return None
    if code.endswith(".SH"):
        symbol = f"sh{code.split('.')[0]}"
    elif code.endswith(".SZ"):
        symbol = f"sz{code.split('.')[0]}"
    else:
        return None

    cache_key = (code, freq, n)
    now = time.monotonic()
    cached = _EM_KLINE_CACHE.get(cache_key)
    if cached and now - cached[0] < _EM_KLINE_CACHE_TTL:
        return [dict(bar) for bar in cached[1]]

    params = {"symbol": symbol, "scale": int(freq), "ma": "no", "datalen": n}
    try:
        response = _sina_session().get(_SINA_KLINE_URL, params=params, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, requests.JSONDecodeError, ValueError) as exc:
        logger.info(
            "sina minute kline failed code=%s freq=%s error=%s",
            code, freq, type(exc).__name__,
        )
        return None
    if not isinstance(payload, list):
        return None

    bars = []
    for row in payload:
        try:
            bars.append({
                "dt": datetime.strptime(row["day"], "%Y-%m-%d %H:%M:%S"),
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "vol": float(row["volume"]),
                "amount": float(row["amount"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    if not bars:
        return None
    _EM_KLINE_CACHE[cache_key] = (now, [dict(bar) for bar in bars])
    return bars


def minute_kline(ts_code: str, freq: str, n: int = 250) -> Optional[list[dict]]:
    """分钟 K 线统一入口：东财主源，新浪备用源。"""
    bars = eastmoney_kline(ts_code, freq=freq, n=n)
    if bars:
        return bars
    bars = sina_minute_kline(ts_code, freq=freq, n=n)
    if bars:
        return bars
    logger.warning(
        "all minute kline sources failed code=%s freq=%s",
        normalize_ts_code(ts_code), freq,
    )
    return None

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


# ── 资金流 / 全市场日线 / 交易日历 ───────────────────────────────────────────
# moneyflow 不含收盘价/涨跌幅/流通市值，需配合 pro.daily / pro.daily_basic 使用。
# 所有金额字段在出口处统一转成「元」（tushare 原始口径为万元）。

_MF_AMOUNT_COLS = [
    "buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
    "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
    "net_mf_amount",
]


@per_day_cache("moneyflow_raw")
def get_moneyflow(trade_date: str) -> str:
    """全市场个股资金流（大/中/小/超大单净额），返回 JSON 字符串。

    主源 tushare moneyflow，akshare 兜底。**所有 *_amount 字段已 *1e4 转成元**
    （tushare 原始为万元口径，不转会令按元写的阈值失效）。
    不注册为 AI 工具——粗筛信号，非 AI 复核工具。
    """
    trade_date = (trade_date or "").replace("-", "")
    df = _moneyflow_tushare(trade_date)
    if df is None or df.empty:
        df = _moneyflow_akshare(trade_date)
        source = "akshare"
    else:
        source = "tushare"
    if df is None or df.empty:
        return json.dumps([], ensure_ascii=False)

    for col in _MF_AMOUNT_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0) * 1e4
    df["source"] = source
    return df.to_json(orient="records", force_ascii=False)


def _moneyflow_tushare(trade_date: str) -> Optional[pd.DataFrame]:
    try:
        df = _tushare().moneyflow(trade_date=trade_date)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df["trade_date"] = df["trade_date"].astype(str)
    return df


def _moneyflow_akshare(trade_date: str) -> Optional[pd.DataFrame]:
    """akshare 东财全市场资金流兜底。主力口径与 tushare 不同（仅"主力"合计）。"""
    import akshare as ak
    try:
        df = ak.stock_individual_fund_flow_rank(indicator="今日")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    rename = {
        "代码": "ts_code", "名称": "name",
        "今日主力净流入-净额": "net_mf_amount",
        "今日超大单净流入-净额": "net_elg_amount",
        "今日大单净流入-净额": "net_lg_amount",
        "今日中单净流入-净额": "net_md_amount",
        "今日小单净流入-净额": "net_sm_amount",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "ts_code" not in df.columns:
        return None
    df["ts_code"] = df["ts_code"].astype(str).map(normalize_ts_code)
    df["trade_date"] = trade_date
    keep = ["ts_code", "name", "trade_date", "net_mf_amount",
            "net_elg_amount", "net_lg_amount", "net_md_amount", "net_sm_amount"]
    return df[[c for c in keep if c in df.columns]]


def get_market_daily(trade_date: str) -> str:
    """全市场日线（含 pct_chg/close/pre_close/vol），返回 JSON 字符串。

    moneyflow 不含收盘价与涨跌幅，靠此补。全市场一次返回。
    """
    trade_date = (trade_date or "").replace("-", "")
    try:
        df = _tushare().daily(trade_date=trade_date)
    except Exception:
        return json.dumps([], ensure_ascii=False)
    if df is None or df.empty:
        return json.dumps([], ensure_ascii=False)
    df["trade_date"] = df["trade_date"].astype(str)
    return df.to_json(orient="records", force_ascii=False)


def get_market_daily_basic(trade_date: str) -> str:
    """全市场每日指标（含 circ_mv 流通市值 / turnover_rate / volume_ratio 量比）。

    circ_mv 为万元口径，出口处 *1e4 转成元。volume_ratio 即量比——moneyflow
    策略可直接用此字段，无需接入 technical 共享缓存。
    """
    trade_date = (trade_date or "").replace("-", "")
    try:
        df = _tushare().daily_basic(trade_date=trade_date)
    except Exception:
        return json.dumps([], ensure_ascii=False)
    if df is None or df.empty:
        return json.dumps([], ensure_ascii=False)
    df["trade_date"] = df["trade_date"].astype(str)
    if "circ_mv" in df.columns:
        df["circ_mv"] = pd.to_numeric(df["circ_mv"], errors="coerce").fillna(0) * 1e4
    return df.to_json(orient="records", force_ascii=False)


def last_n_trade_dates(trade_date: str, n: int = 5) -> list[str]:
    """截至 trade_date 的过去 n 个交易日（含当日，若为交易日）。

    用 tushare trade_cal 拿真实日历（含节假日），避免纯周末跳过漏掉节假日。
    失败时退回纯周末跳过的近似。
    """
    trade_date = (trade_date or "").replace("-", "")
    try:
        end = trade_date
        start = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=n * 3 + 10)).strftime("%Y%m%d")
        cal = _tushare().trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
        # trade_cal 默认按 cal_date 降序返回，需显式升序后再取末尾 n 个
        dates = sorted(cal["cal_date"].astype(str).tolist())
        if len(dates) >= n:
            return dates[-n:]
        return dates  # 不足 n 个就全返
    except Exception:
        d = datetime.strptime(trade_date, "%Y%m%d")
        out = []
        while len(out) < n and d.year > 2000:
            if d.weekday() < 5:
                out.append(d.strftime("%Y%m%d"))
            d -= timedelta(days=1)
        return list(reversed(out))



    """从最近 N 根 K 线计算蜡烛图形态特征。

    返回 dict，包含：
    - latest: 最近一根 K 线的单根形态
    - prev: 倒数第二根 K 线的单根形态（用于识别前日的长上影/长下影等信号）
    - multi_bar: 多蜡烛组合形态（3-5 根）
    """
    if not bars or len(bars) < 3:
        return {}

    def _single_candle(b: dict, prev_closes: list[float] = None) -> dict:
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        amp = h - l
        if amp <= 0:
            return {}
        body = abs(c - o)
        body_pct = round(body / amp * 100, 1)
        upper_shadow = h - max(c, o)
        lower_shadow = min(c, o) - l
        upper_pct = round(upper_shadow / amp * 100, 1)
        lower_pct = round(lower_shadow / amp * 100, 1)
        direction = "阳" if c >= o else "阴"
        is_cross = body_pct <= 10
        is_long_upper = upper_pct >= 50 and body_pct <= 40
        is_long_lower = lower_pct >= 50 and body_pct <= 40
        is_hammer = False
        is_shooting_star = False
        if prev_closes and len(prev_closes) >= 4:
            avg_prev = sum(prev_closes[-5:]) / 5
            if is_long_lower and c < avg_prev:
                is_hammer = True
            if is_long_upper and c > avg_prev:
                is_shooting_star = True
        return {
            "body_pct": body_pct,
            "upper_shadow_pct": upper_pct,
            "lower_shadow_pct": lower_pct,
            "direction": "十字星" if is_cross else direction,
            "is_long_upper_shadow": is_long_upper,
            "is_long_lower_shadow": is_long_lower,
            "is_hammer": is_hammer,
            "is_shooting_star": is_shooting_star,
        }

    all_closes = [float(b["close"]) for b in bars]

    # latest: 最近一根
    latest_candle = _single_candle(bars[-1], all_closes[:-1] if len(bars) >= 6 else all_closes[:-1])

    # prev: 倒数第二根（可能是关键信号 K 线，如 6/10 的长上影）
    prev_candle = {}
    if len(bars) >= 2:
        prev_closes_for_prev = all_closes[:-2] if len(bars) >= 7 else all_closes[:-2]
        prev_candle = _single_candle(bars[-2], prev_closes_for_prev if len(prev_closes_for_prev) >= 5 else None)

    # 多蜡烛组合形态
    patterns: list[str] = []
    if len(bars) >= 5:
        # 双顶风险：最近两根高点接近（相差 < 2%），且中间有低点
        recent_highs = []
        for b in bars[-10:]:
            recent_highs.append(float(b["high"]))
        max_high = max(recent_highs)
        # 找最近的两次接近最高点的位置
        peaks = [i for i, v in enumerate(recent_highs) if v >= max_high * 0.98]
        if len(peaks) >= 2 and peaks[-1] - peaks[0] >= 2:
            patterns.append("双顶雏形")

        # 量价背离：价格新高但量缩
        if len(bars) >= 5:
            last_two = bars[-2:]
            prev_three = bars[-5:-2]
            if float(last_two[-1]["close"]) >= max(float(b["close"]) for b in prev_three):
                avg_vol_recent = sum(float(b["vol"]) for b in last_two) / 2
                avg_vol_prev = sum(float(b["vol"]) for b in prev_three) / 3
                if avg_vol_prev > 0 and avg_vol_recent < avg_vol_prev * 0.7:
                    patterns.append("量价背离(价升量缩)")

        # 连续阴线/阳线计数
        directions_recent = ["阳" if float(b["close"]) >= float(b["open"]) else "阴" for b in bars[-5:]]
        if directions_recent[-1] == "阴":
            consecutive = 0
            for d in reversed(directions_recent):
                if d == "阴":
                    consecutive += 1
                else:
                    break
            if consecutive >= 3:
                patterns.append(f"连续{consecutive}阴")

    # 最近 3 天量价趋势
    last_3_vols = [float(b["vol"]) for b in bars[-3:]]
    last_3_closes = [float(b["close"]) for b in bars[-3:]]
    vol_trend = "放量" if last_3_vols[-1] > sum(last_3_vols[:-1]) / 2 * 1.3 else (
        "缩量" if last_3_vols[-1] < sum(last_3_vols[:-1]) / 2 * 0.7 else "平量"
    )
    price_trend_3d = "上涨" if last_3_closes[-1] > last_3_closes[0] else "下跌"

    return {
        "latest": latest_candle,
        "prev": prev_candle,
        "multi_bar": {
            "patterns": patterns,
            "vol_trend_3d": vol_trend,
            "price_trend_3d": price_trend_3d,
        },
    }


# ── Financial indicator constants & helpers ──────────────────────────────────

# 18 fields selected from tushare fina_indicator (92 available)
_FINA_FIELDS = [
    "end_date",
    # Profitability
    "roe", "roa", "grossprofit_margin", "netprofit_margin", "roic",
    # Per share
    "eps", "bps", "ocfps",
    # Solvency
    "debt_to_assets", "current_ratio", "quick_ratio",
    # Growth YoY
    "or_yoy", "netprofit_yoy", "roe_yoy", "bps_yoy", "basic_eps_yoy",
    # Cash flow / efficiency
    "fcff", "fcfe", "assets_turn",
]


def _safe_number(val):
    """Convert numpy types to native Python, NaN/Inf to None."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
        if isinstance(val, (np.integer,)):
            return int(val)
        if isinstance(val, (np.floating,)):
            if np.isnan(val) or np.isinf(val):
                return None
            return float(val)
        return val
    except Exception:
        return val


def _compute_financial_summary(df: "pd.DataFrame") -> dict:
    """Compute trend labels and warning flags from 4Q financial data.

    df: sorted by end_date descending, columns from _FINA_FIELDS.
    """
    n = len(df)
    if n < 2:
        return {"data_quarters": n, "flags": []}

    latest = df.iloc[0]

    def _trend(series, improving_is_up=True):
        vals = [v for v in series.dropna().tolist() if v is not None]
        if len(vals) < 2:
            return None
        first, last = vals[0], vals[-1]
        diff = first - last
        if abs(diff) < 1.0:
            return "stable"
        if improving_is_up:
            return "improving" if diff > 0 else "declining"
        else:
            return "improving" if diff < 0 else "declining"

    roe_trend = _trend(df["roe_yoy"], improving_is_up=True)
    revenue_trend = _trend(df["or_yoy"], improving_is_up=True)
    margin_trend = _trend(df["grossprofit_margin"], improving_is_up=True)

    # Warning flags
    flags = []

    debt = latest.get("debt_to_assets")
    if debt is not None and debt > 70:
        flags.append(f"资产负债率偏高: {debt:.1f}% (>70%)")

    cr = latest.get("current_ratio")
    if cr is not None and cr < 1.0:
        flags.append(f"流动比率偏低: {cr:.2f} (<1.0)")

    # Check if ALL quarters have negative ROE
    roe_vals = [v for v in df["roe"].dropna().tolist() if v is not None]
    if len(roe_vals) >= 4 and all(v < 0 for v in roe_vals[:4]):
        flags.append("ROE连续4个季度为负")

    # Check if ALL quarters have negative ROE YoY (persistent decline vs last year)
    roe_yoy_vals = [v for v in df["roe_yoy"].dropna().tolist() if v is not None]
    if len(roe_yoy_vals) >= 4 and all(v is not None and v < 0 for v in roe_yoy_vals[:4]):
        flags.append("ROE同比连续4个季度下滑（盈利能力持续恶化）")

    ocfps_vals = [v for v in df["ocfps"].dropna().tolist() if v is not None]
    eps_vals = [v for v in df["eps"].dropna().tolist() if v is not None]
    if len(ocfps_vals) >= 3 and len(eps_vals) >= 3:
        below = sum(
            1 for o, e in zip(ocfps_vals[:3], eps_vals[:3])
            if o < e
        )
        if below >= 3:
            flags.append("经营现金流每股连续3个季度低于每股收益（盈利质量存疑）")

    # Note: trend labels are approximate because fina_indicator data is cumulative
    # within each fiscal year (Q1 < Q2 < Q3 < FY), so comparing different quarters
    # directly is not always apples-to-apples.
    return {
        "data_quarters": n,
        "roe_trend": roe_trend,
        "revenue_growth_trend": revenue_trend,
        "margin_trend": margin_trend,
        "flags": flags,
        "_trend_note": "趋势标签基于最近可用数据的首尾比较，因财务数据为累计值，跨报告期比较可能存在偏差。以 flags（风险标记）为准。",
    }


def get_fundamentals(ts_code: str) -> str:
    """Return valuation + latest 4Q financial indicators + computed summary as JSON.

    Part 1: daily_basic → PE/PB/PS/turnover/circ_mv (always)
    Part 2: fina_indicator → profitability/solvency/growth/cashflow (fail-soft)
    """
    result: dict = {}

    # ── Part 1: Valuation (daily_basic) — always works ────────────────────
    try:
        pro = _tushare()
        today = datetime.today().strftime("%Y%m%d")
        df_val = pro.daily_basic(ts_code=ts_code, trade_date=today,
                                  fields="ts_code,trade_date,pe,pe_ttm,pb,ps_ttm,dv_ttm,turnover_rate,circ_mv")
        if df_val is None or df_val.empty:
            prev = (datetime.today() - timedelta(days=3)).strftime("%Y%m%d")
            df_val = pro.daily_basic(ts_code=ts_code, start_date=prev, end_date=today,
                                      fields="ts_code,trade_date,pe,pe_ttm,pb,ps_ttm,dv_ttm,turnover_rate,circ_mv")
        if df_val is None or df_val.empty:
            return json.dumps({"error": "no fundamental data"})
        val_row = df_val.tail(1).iloc[0].to_dict()
        result["valuation"] = {
            k: _safe_number(val_row.get(k))
            for k in ["trade_date", "pe", "pe_ttm", "pb", "ps_ttm", "dv_ttm", "turnover_rate", "circ_mv"]
        }
    except Exception as e:
        return json.dumps({"error": str(e)})

    # ── Part 2: Financial indicators (fina_indicator) — fail-soft ─────────
    try:
        fields_str = ",".join(_FINA_FIELDS)
        df_fin = pro.fina_indicator(ts_code=ts_code, fields=fields_str)
        if df_fin is not None and not df_fin.empty:
            df_fin = df_fin.sort_values("end_date", ascending=False)
            df_fin = df_fin.drop_duplicates(subset=["end_date"])
            df_fin = df_fin.head(4)
            df_fin = df_fin.where(pd.notna(df_fin), None)
            quarters = []
            for _, row in df_fin.iterrows():
                q = {}
                for col in _FINA_FIELDS:
                    q[col] = _safe_number(row.get(col))
                quarters.append(q)
            result["latest_quarter"] = str(df_fin.iloc[0].get("end_date", ""))
            result["quarters"] = quarters
            result["_note"] = "财务数据为累计报告期值（ROE/ROA等为年初至今累计值，Q1<Q2<Q3<年报）。增长率(or_yoy/roe_yoy等)为同比，可直接用于趋势判断。"
            result["summary"] = _compute_financial_summary(df_fin)
        else:
            result["quarters"] = []
            result["summary"] = None
    except Exception:
        result["quarters"] = []
        result["summary"] = None

    # ── Part 3: trailing PEG（成长股估值用，防 AI 仅凭静态 PE_TTM 偏空误杀高成长标的）──
    # PEG = pe_ttm / 净利润同比增速(%)。仅当增速 > 0 才有意义；负增长/缺失 -> None。
    # 仅供参考：trailing PEG 用历史增速，前瞻判断仍需 AI 用 mx_data_query 查一致预期。
    try:
        pe_ttm = (result.get("valuation") or {}).get("pe_ttm")
        quarters = result.get("quarters") or []
        np_yoy = quarters[0].get("netprofit_yoy") if quarters else None
        peg_ttm = round(pe_ttm / np_yoy, 2) if (pe_ttm and np_yoy and np_yoy > 0) else None
        result["valuation"]["peg_ttm"] = peg_ttm
    except Exception:
        pass

    return json.dumps(result, ensure_ascii=False)


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


# 进程级全量 ts_code→name 缓存。一次 stock_basic 调用拿全 A 股(~5k 行),
# 供 /journal 列表给历史分析条目补中文名用。失败返回 {} 走兜底。
_NAME_MAP_CACHE: Optional[dict] = None


def get_name_map() -> dict:
    """全量 ts_code→中文名 映射(进程级缓存, 一次 tushare 调用)。失败返回 {}。"""
    global _NAME_MAP_CACHE
    if _NAME_MAP_CACHE is not None:
        return _NAME_MAP_CACHE
    try:
        pro = _tushare()
        df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
        if df is None or df.empty:
            _NAME_MAP_CACHE = {}
        else:
            _NAME_MAP_CACHE = {
                str(r["ts_code"]): str(r["name"])
                for _, r in df.iterrows()
                if r.get("ts_code") and r.get("name")
            }
    except Exception:
        _NAME_MAP_CACHE = {}
    return _NAME_MAP_CACHE


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
        opener = _direct_opener()
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
    """Batch fetch latest available daily close (含今天) for multiple stocks.

    ⚠️ 这是「最新可用价」不是「昨收」: 盘后 tushare 发了当日个股 daily(约 15:30)
    后, groupby.last() 取到今日收盘。需要前一交易日收盘(昨收)用 get_prev_close。
    适合「最新价」语义(analyze 的 current_price 等); 当 prev 基准用会吞盈亏。
    """
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


def get_prev_close(ts_codes: list[str]) -> dict[str, Optional[float]]:
    """批量取「昨收」价(前一交易日收盘), 返回 {ts_code: price}。

    与 get_latest_price 的区别: 严格排除今天(trade_date < today)。
    tushare 个股日线收盘后约 15:30 即发布当日数据, 此时 get_latest_price 的
    groupby.last() 会取到今日收盘而非昨收, 导致 OverviewPage 的「今日盈亏」
    cur==prev 恒为 0。本函数取今天之前最近一根日线收盘, 盘中(无今日 daily)
    与盘后(有今日 daily)都正确返回昨收。

    拉取窗口放宽到 10 个自然日, 覆盖节假日后的最近交易日。
    """
    result: dict[str, Optional[float]] = {c: None for c in ts_codes}
    if not ts_codes:
        return result
    try:
        pro = _tushare()
        today = datetime.today().strftime("%Y%m%d")
        start = (datetime.today() - timedelta(days=10)).strftime("%Y%m%d")
        codes_str = ",".join(ts_codes)
        df = pro.daily(ts_code=codes_str, start_date=start, end_date=today,
                        fields="ts_code,trade_date,close")
        if df is None or df.empty:
            return result
        # 排除今天, 取每只股票最近一根日线收盘
        df = df[df["trade_date"].astype(str) < today]
        if df.empty:
            return result
        latest = df.sort_values("trade_date").groupby("ts_code").last()
        for code in ts_codes:
            if code in latest.index:
                result[code] = float(latest.loc[code, "close"])
    except Exception:
        pass
    return result


def _intraday_shape(open_p: float, last: float, prev_close: Optional[float],
                    high: float, low: float) -> str:
    """根据开盘/现价/昨收/高低点判断盘中走势形态文字。"""
    if not prev_close or prev_close <= 0 or open_p <= 0:
        return "未知"
    open_chg = (open_p - prev_close) / prev_close
    trend = (last - open_p) / open_p
    open_label = "高开" if open_chg > 0.002 else ("低开" if open_chg < -0.002 else "平开")
    trend_label = "高走" if trend > 0.003 else ("低走" if trend < -0.003 else "震荡")
    base = f"{open_label}{trend_label}"
    # 冲高回落 / 探底回升补充
    if last > 0:
        upper_excursion = (high - last) / last  # 盘中最高到现价的回落
        lower_excursion = (last - low) / last if low > 0 else 0
        if upper_excursion > 0.01 and trend < 0.003:
            base += "（冲高回落）"
        elif lower_excursion > 0.01 and trend > -0.003:
            base += "（探底回升）"
    return base


def _tx_intraday_bars(ts_code: str) -> Optional[dict]:
    """腾讯分时(明文JSON,HTTPS,不限流,可并发,含昨收/名)。当日分时。

    secid: SH->sh, SZ->sz; BJ->bj(失败 fallback akshare)。
    分时点 "HHMM price cumvol cumamount" 差分为单分钟; 昨收 qt[4]/名 qt[1]。
    失败返 None。
    """
    try:
        symbol = ts_code.split(".")[0]
        suffix = ts_code.split(".")[1] if "." in ts_code else ""
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix)
        if not prefix:
            return None
        code = f"{prefix}{symbol}"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        opener = _direct_opener()
        with opener.open(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        blk = (payload.get("data") or {}).get(code) or {}
        data_blk = blk.get("data") or {}
        pts = data_blk.get("data") or []
        if not pts:
            return None
        date = data_blk.get("date") or datetime.today().strftime("%Y-%m-%d")
        if len(date) == 8 and "-" not in date:  # 腾讯返回 "20260710" -> "2026-07-10"
            date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        qt = (blk.get("qt") or {}).get(code) or []
        prev_close = float(qt[4]) if len(qt) > 4 and qt[4] else None
        name = qt[1] if len(qt) > 1 else None

        bars = []
        prev_vol = 0.0
        prev_amt = 0.0
        for pt in pts:
            parts = pt.split()
            if len(parts) < 4:
                continue
            hhmm = parts[0]
            price = float(parts[1])
            cumvol = float(parts[2])
            cumamt = float(parts[3])
            bars.append({
                "time": f"{date} {hhmm[:2]}:{hhmm[2:4]}:00",
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "vol": (cumvol - prev_vol) * 100,  # 手 -> 股(与 akshare 一致, 前端 VWAP=amount/vol 元/股)
                "amount": cumamt - prev_amt,
            })
            prev_vol, prev_amt = cumvol, cumamt
        if not bars:
            return None
        last_time = bars[-1]["time"]
        now = datetime.today()
        is_intraday = (date == now.strftime("%Y-%m-%d")) and (last_time[11:16] < "15:00")
        return {
            "trade_date": date.replace("-", ""),
            "as_of_time": last_time,
            "is_intraday": is_intraday,
            "prev_close": round(prev_close, 2) if prev_close else None,
            "prev_vol_shou": None,
            "name": name,
            "bars": bars,
        }
    except Exception:
        return None


def _prev_vol_from_daily(ts_code: str, trade_date_norm: str) -> Optional[float]:
    """从日线(不复权)取昨日量(手),供 AI 量比。失败返 None。"""
    try:
        dbars = json.loads(get_daily_price(ts_code, adj="none"))
        prev_bars = [b for b in dbars if str(b.get("trade_date")) < trade_date_norm]
        if prev_bars:
            return float(prev_bars[-1].get("vol") or 0)
    except Exception:
        pass
    return None


def get_intraday_bars(ts_code: str, trade_date: Optional[str] = None,
                      with_prev_vol: bool = True) -> dict:
    """底层：取个股 1 分钟分时 + 昨收 (+ 昨量)。

    数据源：当日优先腾讯分时(明文JSON,不限流,可并发,含昨收/名)；历史日或腾讯失败
    fallback akshare(stock_zh_a_minute,V8 需 _AKSHARE_LOCK 串行)。

    trade_date:
      - None：当日
      - "YYYYMMDD"/"YYYY-MM-DD"：历史交易日(仅 akshare 可查,最近约5-8日)

    with_prev_vol: 是否补取 prev_vol_shou(昨日量,AI 量比用)。看板传 False 省一次日线;
                   AI 路径(get_intraday_snapshot)默认 True。

    供 get_intraday_snapshot(AI 特征)和分时看板复用。
    返回 dict：{trade_date, as_of_time, is_intraday, prev_close, prev_vol_shou, [name], bars}
    任何失败返回空 dict(fail-soft)。bars 按时间升序,仅含选定日。
    """
    # ── 当日: 优先腾讯(快, 含昨收/名, 可并发) ──
    today_str = datetime.today().strftime("%Y-%m-%d")
    want_today = (not trade_date) or (trade_date.replace("-", "") == today_str.replace("-", ""))
    if want_today:
        tx = _tx_intraday_bars(ts_code)
        if tx and tx.get("bars"):
            if with_prev_vol:
                tx["prev_vol_shou"] = _prev_vol_from_daily(ts_code, tx["trade_date"])
            return tx
        # 腾讯失败(BJ/接口异常) -> fallback akshare

    # ── fallback: akshare(历史日 或 腾讯失败) ──
    out: dict = {}
    try:
        import akshare as ak
        symbol = ts_code.split(".")[0]
        suffix = ts_code.split(".")[1] if "." in ts_code else ""
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix, "sh")
        with _AKSHARE_LOCK:  # 序列化 mini_racer(V8) 初始化, 防并发 FATAL
            df = ak.stock_zh_a_minute(symbol=f"{prefix}{symbol}", period="1", adjust="")
        if df is None or df.empty:
            return out
        df = df.copy()
        df["day"] = df["day"].astype(str)

        # 选定日期：传入则归一为 YYYY-MM-DD 并过滤，否则取 akshare 最新一天
        if trade_date:
            sel = trade_date.replace("-", "")
            sel_date = f"{sel[:4]}-{sel[4:6]}-{sel[6:8]}" if len(sel) == 8 else trade_date
            today_df = df[df["day"].str.startswith(sel_date)].reset_index(drop=True)
            last_date = sel_date
        else:
            last_date = df["day"].iloc[-1][:10]
            today_df = df[df["day"].str.startswith(last_date)].reset_index(drop=True)
        if today_df.empty:
            return out
        for col in ("open", "high", "low", "close", "volume"):
            if col in today_df.columns:
                today_df[col] = pd.to_numeric(today_df[col], errors="coerce")
        # amount 列在不同 akshare 版本可能缺失，缺失时用 close*volume 估算成交额
        if "amount" in today_df.columns:
            today_df["amount"] = pd.to_numeric(today_df["amount"], errors="coerce")
            today_df["amount"] = today_df["amount"].fillna(today_df["close"] * today_df["volume"])
        else:
            today_df["amount"] = today_df["close"] * today_df["volume"]

        last_row = today_df.iloc[-1]
        as_of_time = str(last_row["day"])
        trade_date_norm = last_date.replace("-", "")
        now = datetime.today()
        is_intraday = (last_date == now.strftime("%Y-%m-%d")) and (as_of_time[11:16] < "15:00")

        # 昨收 + 昨量: 从日线取(口径正确, 历史日也准; 替代旧新浪昨收 + or True 浪费)
        prev_close: Optional[float] = None
        prev_vol_shou: Optional[float] = None
        try:
            dbars = json.loads(get_daily_price(ts_code, adj="none"))
            prev_bars = [b for b in dbars if str(b.get("trade_date")) < trade_date_norm]
            if prev_bars:
                prev_close = float(prev_bars[-1]["close"])
                if with_prev_vol:
                    prev_vol_shou = float(prev_bars[-1].get("vol") or 0)
        except Exception:
            pass

        bars_list = []
        for _, r in today_df.iterrows():
            bars_list.append({
                "time": str(r["day"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "vol": float(r["volume"]),
                "amount": float(r["amount"]),
            })

        out = {
            "trade_date": trade_date_norm,
            "as_of_time": as_of_time,
            "is_intraday": is_intraday,
            "prev_close": round(prev_close, 2) if prev_close else None,
            "prev_vol_shou": prev_vol_shou,
            "bars": bars_list,
        }
    except Exception:
        pass
    return out

def _intraday_session_segments(bars: list[dict], prev_close: Optional[float]) -> Optional[list[dict]]:
    """量价段分析：把当日分为早盘(09:30-10:30)/中盘(10:30-11:30,13:00-14:00)/尾盘(14:00-15:00)。

    每段算：起止价涨跌%（相对段开盘）、成交量占全天比。返回 3 段摘要。
    """
    if not bars or not prev_close:
        return None

    def _seg(bars_seg: list[dict], name: str) -> dict:
        if not bars_seg:
            return {"name": name, "chg_pct": None, "vol_pct": None}
        seg_open = bars_seg[0]["close"]
        seg_close = bars_seg[-1]["close"]
        chg = (seg_close - seg_open) / seg_open * 100 if seg_open > 0 else None
        vol = sum(b["vol"] for b in bars_seg)
        return {"name": name, "chg_pct": round(chg, 2) if chg is not None else None,
                "close": round(seg_close, 2), "vol": vol}

    total_vol = sum(b["vol"] for b in bars) or 1
    early, mid, late = [], [], []
    for b in bars:
        hm = b["time"][11:16]
        if "09:30" <= hm < "10:30":
            early.append(b)
        elif "10:30" <= hm < "11:30" or "13:00" <= hm < "14:00":
            mid.append(b)
        elif "14:00" <= hm <= "15:00":
            late.append(b)
    segs = [_seg(early, "早盘"), _seg(mid, "中盘"), _seg(late, "尾盘")]
    for s in segs:
        s["vol_pct"] = round(s["vol"] / total_vol * 100, 1) if s.get("vol") else None
        s.pop("vol", None)
    return segs


def _intraday_swing(bars: list[dict], prev_close: Optional[float]) -> Optional[dict]:
    """盘中拐点/回吐：找盘中最高/最低点，算从极值到现价的回吐/反弹幅度。

    返回 {high_time, high_pct, high_to_now_pct, low_time, low_pct, low_to_now_pct, verdict}
    high_pct/low_pct 相对昨收；*_to_now_pct 为从极值到最新价的变动（正=从高点回吐，负=从低点反弹）。
    """
    if not bars or not prev_close:
        return None
    last_price = bars[-1]["close"]
    # 盘中最高/最低用 close 序列
    closes = [(b["time"], b["close"]) for b in bars]
    hi_time, hi_price = max(closes, key=lambda x: x[1])
    lo_time, lo_price = min(closes, key=lambda x: x[1])
    hi_pct = (hi_price - prev_close) / prev_close * 100
    lo_pct = (lo_price - prev_close) / prev_close * 100
    hi_to_now = (last_price - hi_price) / hi_price * 100  # 负值=从高点回落
    lo_to_now = (last_price - lo_price) / lo_price * 100  # 正值=从低点反弹
    # 判定：现价离高点近还是离低点近
    verdict = ""
    if hi_to_now < -1.0:
        verdict = f"从盘中高点({hi_pct:+.1f}%)回落{abs(hi_to_now):.1f}%"
    elif lo_to_now > 1.0:
        verdict = f"从盘中低点({lo_pct:+.1f}%)反弹{lo_to_now:.1f}%"
    else:
        verdict = "现价接近盘中极值，趋势延续"
    return {
        "high_time": hi_time[11:16], "high_pct": round(hi_pct, 2),
        "high_to_now_pct": round(hi_to_now, 2),
        "low_time": lo_time[11:16], "low_pct": round(lo_pct, 2),
        "low_to_now_pct": round(lo_to_now, 2),
        "verdict": verdict,
    }


def _intraday_vol_price_match(bars: list[dict]) -> Optional[dict]:
    """量价配合：统计放量上涨 vs 放量下跌的分钟数，判断主动买/卖盘主导。

    放量阈值 = 当日分钟均量的 1.5 倍。上涨/下跌按当根 close vs open。
    返回 {up_vol_mins, down_vol_mins, verdict} verdict ∈ {主动买盘主导/主动卖盘主导/多空均衡}。
    """
    if not bars or len(bars) < 10:
        return None
    avg_vol = sum(b["vol"] for b in bars) / len(bars)
    threshold = avg_vol * 1.5
    if threshold <= 0:
        return None
    up_mins = down_mins = 0
    for b in bars:
        if b["vol"] < threshold:
            continue
        if b["close"] > b["open"]:
            up_mins += 1
        elif b["close"] < b["open"]:
            down_mins += 1
    total = up_mins + down_mins
    if total == 0:
        verdict = "无明显放量分钟，多空均衡"
    elif up_mins > down_mins * 1.5:
        verdict = f"主动买盘主导（放量上涨{up_mins}分钟 vs 放量下跌{down_mins}分钟）"
    elif down_mins > up_mins * 1.5:
        verdict = f"主动卖盘主导（放量下跌{down_mins}分钟 vs 放量上涨{up_mins}分钟）"
    else:
        verdict = f"多空均衡（放量上涨{up_mins}分钟 vs 放量下跌{down_mins}分钟）"
    return {"up_vol_mins": up_mins, "down_vol_mins": down_mins, "verdict": verdict}


def get_intraday_snapshot(ts_code: str) -> str:
    """获取个股当日盘中分时走势快照（akshare 1分钟线 + 新浪昨收，Python 预计算特征）。

    盘中时为实时走势快照，收盘后为当日完整走势。fail-soft：任何失败返回空 dict。
    与 get_realtime_price 不同，这里返回"走势特征"（开盘/最高/最低/VWAP/形态/量能 + 量价段/拐点/量价配合），
    供 analyze.py 注入 prompt，不注册为 AI tool（避免漏调，与 get_market_context 同设计）。

    返回 JSON 字符串。注意：分时数据为真实价（不复权），昨收取新浪真实价，口径一致。
    """
    result: dict = {}
    raw = get_intraday_bars(ts_code)
    if not raw or not raw.get("bars"):
        return json.dumps(result, ensure_ascii=False)
    try:
        bars = raw["bars"]
        prev_close = raw.get("prev_close")
        prev_vol_shou = raw.get("prev_vol_shou")
        as_of_time = raw["as_of_time"]
        trade_date = raw["trade_date"]
        is_intraday = raw["is_intraday"]

        last_price = bars[-1]["close"]
        open_price = bars[0]["open"]
        high = max(b["high"] for b in bars)
        low = min(b["low"] for b in bars)
        total_amount = sum(b["amount"] for b in bars)
        total_vol_shares = sum(b["vol"] for b in bars)
        total_vol_shou = total_vol_shares / 100.0  # 股 → 手
        vwap = (total_amount / total_vol_shares) if total_vol_shares > 0 else last_price

        day_chg_pct = ((last_price - prev_close) / prev_close * 100) if prev_close else None
        vwap_position_pct = ((last_price - vwap) / vwap * 100) if vwap > 0 else None
        amplitude_pct = ((high - low) / prev_close * 100) if prev_close else None

        # 量比：当前累计量 / 昨日全天量，按时间进度修正（收盘后 progress=1 即正常量比）
        vol_ratio: Optional[float] = None
        vol_label: Optional[str] = None
        if prev_vol_shou and prev_vol_shou > 0 and len(bars) > 0:
            progress = len(bars) / 240.0
            if progress > 0.02:
                vol_ratio = round((total_vol_shou / prev_vol_shou) / progress, 2)
                if vol_ratio >= 1.2:
                    vol_label = "放量"
                elif vol_ratio <= 0.8:
                    vol_label = "缩量"
                else:
                    vol_label = "平量"

        shape = _intraday_shape(open_price, last_price, prev_close, high, low)
        segments = _intraday_session_segments(bars, prev_close)
        swing = _intraday_swing(bars, prev_close)
        vol_price = _intraday_vol_price_match(bars)

        result = {
            "trade_date": trade_date,
            "as_of_time": as_of_time,
            "is_intraday": is_intraday,
            "last_price": round(last_price, 2),
            "prev_close": round(prev_close, 2) if prev_close else None,
            "open": round(open_price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "day_chg_pct": round(day_chg_pct, 2) if day_chg_pct is not None else None,
            "vwap": round(vwap, 2),
            "vwap_position_pct": round(vwap_position_pct, 2) if vwap_position_pct is not None else None,
            "amplitude_pct": round(amplitude_pct, 2) if amplitude_pct is not None else None,
            "amount_yi": round(total_amount / 1e8, 2),
            "vol_ratio": vol_ratio,
            "vol_label": vol_label,
            "shape": shape,
            "session_segments": segments,
            "swing": swing,
            "vol_price_match": vol_price,
        }
    except Exception:
        pass
    return json.dumps(result, ensure_ascii=False)


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
    entity = " ".join(part for part in (name, code_short, ts_code) if part)

    if category == "general":
        q = query or (
            f"{entity} 公告 研报 新闻" if entity
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
        actual_name = entity or code_short
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
        opener = _direct_opener()
        with opener.open(req, timeout=30) as resp:
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
        })
    from apex.evidence import normalize_search_results
    results, quality = normalize_search_results(
        results, ts_code=ts_code, name=name, category=category,
    )
    return json.dumps({
        "source": "bocha:web-search",
        "category": category,
        "query": q,
        "freshness": actual_freshness,
        "count": len(results),
        "results": results,
        "quality": quality,
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
                fields="trade_date,close,vol,pct_chg,amount",
            )
            if df is None or df.empty:
                continue
            df = df.sort_values("trade_date").reset_index(drop=True)
            result[code] = df.tail(days).to_dict(orient="records")
        except Exception:
            continue
    return result


def get_index_daily(code: str, days: int = 2) -> str:
    """取指数日线(ED13 端点), 返回 JSON 字符串。

    Args:
        code: 指数代码(000001.SH 上证 / 399001.SZ 深证)
        days: 取最近 N 天(默认 2, 够算当日 vol + 昨收对比)

    Returns:
        JSON 字符串: {
            "code": "000001.SH",
            "name": "上证指数",  # 已知指数直接给名字, 不另查
            "bars": [{"trade_date": "20260625", "close": 3245.67, "vol": 12345678, "pct_chg": 0.79}, ...]
        }
        失败返 "{}" 字符串(后端 parse_json 解包成空 dict)
    """
    INDEX_NAMES = {"000001.SH": "上证指数", "399001.SZ": "深证成指", "399006.SZ": "创业板指"}
    payload = {"code": code, "name": INDEX_NAMES.get(code, ""), "bars": []}
    bars_map = _fetch_index_bars([code], days=days)
    bars = bars_map.get(code, [])
    if not bars:
        return json.dumps(payload, ensure_ascii=False)
    # 把 tushare 字段标准化(原样返回, 字段名是 trade_date/close/vol/pct_chg)
    payload["bars"] = bars
    return json.dumps(payload, ensure_ascii=False)


def get_index_daily_batch(codes: list[str], days: int = 2) -> str:
    """批量取指数日线, 返回 JSON 字符串 { [code]: { code, name, bars } }。

    用于市场温度卡一次拉多只指数。失败的 code 对应空 bars。
    bars 字段: trade_date / close / vol / pct_chg / amount(千元)。
    """
    INDEX_NAMES = {
        "000001.SH": "上证指数", "399001.SZ": "深证成指", "399006.SZ": "创业板指",
        "000300.SH": "沪深300", "000905.SH": "中证500", "000688.SH": "科创50",
        "399106.SZ": "深证综指",
    }
    bars_map = _fetch_index_bars(codes, days=days)
    result: dict[str, dict] = {}
    for c in codes:
        result[c] = {"code": c, "name": INDEX_NAMES.get(c, ""), "bars": bars_map.get(c, [])}
    return json.dumps(result, ensure_ascii=False)


def get_index_realtime_batch(codes: list[str]) -> str:
    """批量取指数实时行情(新浪), 返回 JSON 字符串
    { [code]: { code, close, prev_close, pct_chg, amount, trade_date } }。

    用于市场温度卡在 tushare EOD 日线尚未发布当日数据时兜底:
    盘中 + 盘后到 EOD 发布前(约 16:00-17:00)这段窗口, 显示当日指数点位
    + 涨跌幅 + 成交额, 而不是前一交易日。

    新浪指数字段: [2]=昨收 [3]=最新 [9]=成交额(元) [30]=日期(YYYY-MM-DD)。
    amount 统一换算成千元(元 ÷1000), 与 tushare index_daily.amount 单位一致,
    前端 amountKToYi(千元→亿) 统一处理。失败的 code 对应空 dict {}。
    """
    import re
    result: dict[str, dict] = {c: {} for c in codes}
    if not codes:
        return json.dumps(result, ensure_ascii=False)
    sina_codes = []
    for c in codes:
        symbol = c.split(".")[0]
        if c.endswith(".SH"):
            sina_codes.append(f"sh{symbol}")
        elif c.endswith(".SZ"):
            sina_codes.append(f"sz{symbol}")
    if not sina_codes:
        return json.dumps(result, ensure_ascii=False)
    url = f"https://hq.sinajs.cn/list={','.join(sina_codes)}"
    try:
        req = urllib.request.Request(url, headers={
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0",
        })
        # 显式跳过系统代理(新浪在国内, 外代理会断连)
        opener = _direct_opener()
        with opener.open(req, timeout=10) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
    except Exception:
        return json.dumps(result, ensure_ascii=False)

    symbol_to_ts = {c.split(".")[0]: c for c in codes}
    pattern = re.compile(r'var hq_str_(sh|sz)(\d{6})="([^"]*)";')
    for line in raw.split("\n"):
        m = pattern.match(line.strip())
        if not m:
            continue
        symbol = m.group(2)
        fields = m.group(3).split(",")
        if len(fields) < 10:
            continue
        ts_code = symbol_to_ts.get(symbol)
        if not ts_code:
            continue
        try:
            prev_close = float(fields[2])
            close = float(fields[3])
            amount_yuan = float(fields[9])
        except (ValueError, IndexError):
            continue
        if close <= 0:
            continue
        pct_chg = (
            round((close - prev_close) / prev_close * 100, 4)
            if prev_close > 0 else None
        )
        raw_date = fields[30] if len(fields) > 30 else ""
        trade_date = raw_date.replace("-", "") if raw_date else ""
        result[ts_code] = {
            "code": ts_code,
            "close": close,
            "prev_close": prev_close,
            "pct_chg": pct_chg,
            "amount": amount_yuan / 1000.0,  # 元 → 千元, 对齐 tushare
            "trade_date": trade_date,
        }
    return json.dumps(result, ensure_ascii=False)


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


# ── 市场情绪面评分（A 股语境：涨停/跌停/炸板/连板 -> 三维度 score + regime + market_style）──
# 设计：原始数据作证据，三维度 score+level 供 AI 直接引用，regime/market_style 是 Python 预计算结论。
# 调阈值只改下面的分段断点常量，不动判断逻辑。新增数据源（涨跌家数/两融）时扩 _score_* 即可，不动 prompt。

# 分段线性断点 [(x, y)]：x=原始值, y=0-100 分。升序。
_BR_LIMIT_UP = [(0, 0), (20, 25), (40, 50), (60, 70), (100, 100)]            # 涨停家数 -> 广度
_BR_CONSEC = [(0, 0), (3, 30), (5, 60), (7, 80), (10, 100)]                  # 最高连板 -> 接力
_BR_STRONG_POOL = [(0, 0), (30, 30), (60, 60), (100, 100)]                   # 强势股池家数 -> 接力厚度
_BR_LIMIT_DOWN = [(0, 100), (20, 75), (50, 40), (100, 15), (200, 0)]         # 跌停家数 -> 风险偏好(反向)
_BR_BROKEN_RATE = [(0, 100), (20, 70), (40, 40), (60, 10), (100, 0)]         # 炸板率% -> 风险偏好(反向)
_BR_UP_DOWN_RATIO = [(0, 0), (0.3, 20), (1, 50), (2, 80), (3, 100)]          # 涨停/跌停比 -> 风险偏好


def _piecewise_score(value: float, breaks) -> int:
    """分段线性映射。breaks = [(x0,y0),...] 升序，value 超出两端夹断。"""
    if value <= breaks[0][0]:
        return max(0, breaks[0][1])
    if value >= breaks[-1][0]:
        return breaks[-1][1]
    for i in range(len(breaks) - 1):
        x0, y0 = breaks[i]
        x1, y1 = breaks[i + 1]
        if x0 <= value <= x1:
            if x1 == x0:
                return y1
            return round(y0 + (y1 - y0) * (value - x0) / (x1 - x0))
    return breaks[-1][1]


def _level_from_score(score: int) -> str:
    """score -> strong/mid/weak 三档（> =67 strong, >=34 mid, else weak）。"""
    if score >= 67:
        return "strong"
    if score >= 34:
        return "mid"
    return "weak"


def _score_breadth(limit_up_count: int) -> dict:
    s = _piecewise_score(limit_up_count, _BR_LIMIT_UP)
    return {"score": s, "level": _level_from_score(s)}


def _score_momentum(max_consecutive: int, strong_count: int) -> dict:
    s = round(_piecewise_score(max_consecutive, _BR_CONSEC) * 0.6
              + _piecewise_score(strong_count, _BR_STRONG_POOL) * 0.4)
    # 连板是接力核心信号：连板<=3 强制 weak（强势股池的"60日新高"不算真接力）
    if max_consecutive <= 3:
        s = min(s, 33)
    return {"score": s, "level": _level_from_score(s)}


def _score_risk_appetite(limit_down_count: int, broken_rate: float,
                         up_down_ratio) -> dict:
    ratio_val = up_down_ratio if up_down_ratio is not None else 0
    s = round(_piecewise_score(limit_down_count, _BR_LIMIT_DOWN) * 0.4
              + _piecewise_score(broken_rate, _BR_BROKEN_RATE) * 0.3
              + _piecewise_score(ratio_val, _BR_UP_DOWN_RATIO) * 0.3)
    return {"score": s, "level": _level_from_score(s)}


def _map_regime(total_score: int) -> str:
    """total_score -> 五档情绪温度计（>=80 亢奋 / >=60 偏热 / >=40 中性 / >=20 偏冷 / else 恐慌）。"""
    if total_score >= 80:
        return "亢奋"
    if total_score >= 60:
        return "偏热"
    if total_score >= 40:
        return "中性"
    if total_score >= 20:
        return "偏冷"
    return "恐慌"


def _infer_market_style(breadth_lv: str, momentum_lv: str,
                        risk_lv: str, risk_score: int) -> str:
    """三维度档位组合 -> A 股语境市场风格（优先级规则）。

    与 regime 正交：regime 是连续温度计，market_style 是离散结构。
    可同时「偏冷+抱团」（少数高标撑着大盘冷）。
    """
    if breadth_lv == "weak" and momentum_lv == "weak" and risk_score < 15:
        return "冰点"
    if breadth_lv == "strong" and momentum_lv == "strong" and risk_lv == "strong":
        return "高潮"
    if momentum_lv == "strong" and breadth_lv == "weak":
        return "抱团"
    if momentum_lv == "weak" and breadth_lv == "weak":
        return "退潮"
    if breadth_lv == "mid" and momentum_lv == "weak":
        return "修复"
    return "轮动"


def _fetch_market_sentiment(ts_code: str):
    """大盘情绪面（东财涨停/跌停/炸板/强势股池）。

    注入 get_market_context，不注册为 AI tool（情绪面是公共背景，每只股都要看）。
    4 个池子都是纯 HTTP JSON（无 V8），不需 _AKSHARE_LOCK。
    NO_PROXY eastmoney 直连（复用筹码峰的坑）。单日调一次约 1s。fail-soft 返 None。
    """
    try:
        import akshare as ak
    except ImportError:
        return None

    today = datetime.today().strftime("%Y%m%d")
    symbol = ts_code.split(".")[0]

    _np_orig = (os.environ.get("NO_PROXY"), os.environ.get("no_proxy"))
    os.environ["NO_PROXY"] = ((_np_orig[0] or "") + ",.eastmoney.com").lstrip(",")
    os.environ["no_proxy"] = ((_np_orig[1] or "") + ",.eastmoney.com").lstrip(",")

    try:
        limit_up_df = ak.stock_zt_pool_em(date=today)
        limit_down_df = ak.stock_zt_pool_dtgc_em(date=today)
        broken_df = ak.stock_zt_pool_zbgc_em(date=today)
        strong_df = ak.stock_zt_pool_strong_em(date=today)
    except Exception:
        return None
    finally:
        for _k, _v in zip(("NO_PROXY", "no_proxy"), _np_orig):
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

    # 非交易日 / 盘前：涨停池空 -> 无情绪数据
    if limit_up_df is None or limit_up_df.empty:
        return None

    limit_up_count = len(limit_up_df)
    limit_down_count = len(limit_down_df) if limit_down_df is not None else 0
    broken_count = len(broken_df) if broken_df is not None else 0
    strong_count = len(strong_df) if strong_df is not None else 0

    lian_col = [c for c in limit_up_df.columns if "连板" in c]
    max_consecutive = int(limit_up_df[lian_col[0]].max()) if lian_col else 0

    denom = limit_up_count + broken_count
    broken_rate = broken_count / denom * 100 if denom else 0.0
    up_down_ratio = (limit_up_count / limit_down_count) if limit_down_count else None

    # 个股自身今天是否在池中（标的涨停/在强势股池 = 个股情绪强信号，尤其题材/游资）
    stock_in_pool = {"limit_up": False, "strong_pool": False, "consecutive": None}
    if "代码" in limit_up_df.columns:
        codes_up = limit_up_df["代码"].astype(str).values
        if lian_col and symbol in codes_up:
            stock_in_pool["limit_up"] = True
            stock_in_pool["consecutive"] = int(
                limit_up_df.loc[limit_up_df["代码"].astype(str) == symbol, lian_col[0]].iloc[0])
    if strong_df is not None and not strong_df.empty and "代码" in strong_df.columns:
        stock_in_pool["strong_pool"] = symbol in strong_df["代码"].astype(str).values

    breadth = _score_breadth(limit_up_count)
    momentum = _score_momentum(max_consecutive, strong_count)
    risk_appetite = _score_risk_appetite(limit_down_count, broken_rate, up_down_ratio)

    total_score = round((breadth["score"] + momentum["score"] + risk_appetite["score"]) / 3)
    regime = _map_regime(total_score)
    market_style = _infer_market_style(
        breadth["level"], momentum["level"], risk_appetite["level"], risk_appetite["score"])

    reasons = [
        f"涨停{limit_up_count}家/跌停{limit_down_count}家"
        + (f"，涨停/跌停比{up_down_ratio:.2f}" if up_down_ratio is not None else ""),
        f"炸板率{broken_rate:.0f}%",
        f"最高连板{max_consecutive}板，强势股池{strong_count}家",
        f"广度{breadth['level']}({breadth['score']})/接力{momentum['level']}({momentum['score']})/风险偏好{risk_appetite['level']}({risk_appetite['score']})",
    ]
    if stock_in_pool["limit_up"] or stock_in_pool["strong_pool"]:
        tags = []
        if stock_in_pool["limit_up"]:
            tags.append("今日涨停")
        if stock_in_pool["strong_pool"]:
            tags.append("在强势股池")
        reasons.append("标的自身" + "+".join(tags))

    return {
        "as_of": datetime.today().strftime("%Y-%m-%d"),
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "broken_limit_count": broken_count,
        "broken_rate_pct": round(broken_rate, 1),
        "max_consecutive": max_consecutive,
        "strong_pool_count": strong_count,
        "up_down_ratio": round(up_down_ratio, 2) if up_down_ratio is not None else None,
        "stock_in_pool": stock_in_pool,
        "breadth": breadth,
        "momentum": momentum,
        "risk_appetite": risk_appetite,
        "total_score": total_score,
        "regime": regime,
        "market_style": market_style,
        "reasons": reasons,
    }


def get_market_context(ts_code: str) -> str:
    """大盘 + 板块 + 资金面 + 个股相对 综合 context。

    返回 JSON 字符串。每个 section 独立 fail-soft，单项失败不影响其他。
    设计上仅给 analyze.py 调用、不注册为 AI tool（避免 AI 漏调或乱调）。
    """
    result: dict = {
        "as_of": datetime.today().strftime("%Y-%m-%d"),
        "indices": [],
        "index_intraday": None,  # 大盘当日分时走势（HS300 压缩特征，补日线只有收盘涨跌的缺口）
        "sector": None,
        "north_money": None,
        "stock_relative": None,
        "market_sentiment": None,  # 市场情绪面（涨停/跌停/炸板/连板 -> 三维度 score + regime + market_style）
    }

    # 1. 大盘指数（日级聚合）
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

    # 1b. 大盘当日分时走势（沪深300）—— 复用个股分时压缩链（akshare stock_zh_a_minute
    #     对指数符号同样可用），把 1 分钟 bars 压成分段/拐点/量价特征注入，原始 bars 不落盘。
    #     非交易时段/失败 fail-soft 返 None，analyze 侧跳过该段。
    try:
        snap = json.loads(get_intraday_snapshot("000300.SH"))
        if snap and snap.get("last_price"):
            # 指数点位是市值加权，amount/vol 算不出点位口径的 VWAP
            # （实测 vwap≈31.6 vs 点位≈4608，量级差 ~146 倍）——抹掉避免垃圾值落 trace/journal
            snap["vwap"] = None
            snap["vwap_position_pct"] = None
            result["index_intraday"] = snap
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

    # 5. 市场情绪面（涨停/跌停/炸板/连板 -> 三维度 score + regime + market_style）
    try:
        result["market_sentiment"] = _fetch_market_sentiment(ts_code)
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
    # 妙想 MX API tools
    "mx_data_query": _mx.mx_data_query,
    "mx_news_search": _mx.mx_news_search,
    "mx_stock_screen": _mx.mx_stock_screen,
}
