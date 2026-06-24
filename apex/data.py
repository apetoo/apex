"""
Data layer: tushare + akshare + bocha. Functions here are also registered as tools
for the Claude API agent in analyze.py.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from apex import mx_client as _mx

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


def compute_candlestick_features(bars: list[dict]) -> dict:
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


def _sina_prev_close(ts_code: str) -> Optional[float]:
    """从新浪行情取昨收价（真实价，不复权）。失败返 None。

    与 akshare 分时线口径一致（均为真实成交价），避免用 qfq 日线昨收导致除权日算错。
    """
    try:
        symbol = ts_code.split(".")[0]
        suffix = ts_code.split(".")[1] if "." in ts_code else ""
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix, "sh")
        url = f"https://hq.sinajs.cn/list={prefix}{symbol}"
        req = urllib.request.Request(url, headers={
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0",
        })
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
        import re
        m = re.search(r'="([^"]*)"', raw)
        if not m:
            return None
        fields = m.group(1).split(",")
        if len(fields) < 3:
            return None
        pc = float(fields[2])
        return pc if pc > 0 else None
    except Exception:
        return None


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


def get_intraday_bars(ts_code: str) -> dict:
    """底层：取个股当日 1 分钟 K 线（akshare，真实价不复权）+ 昨收 + 昨量。

    供 get_intraday_snapshot（给 AI 的特征）和 app.py 分时图（给人看）复用。
    返回 dict：{trade_date, as_of_time, is_intraday, prev_close, prev_vol_shou,
               bars: [{time, open, high, low, close, vol, amount}, ...]}
    任何失败返回空 dict（fail-soft）。bars 按时间升序，仅含当日。
    """
    out: dict = {}
    try:
        import akshare as ak
        symbol = ts_code.split(".")[0]
        suffix = ts_code.split(".")[1] if "." in ts_code else ""
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix, "sh")
        df = ak.stock_zh_a_minute(symbol=f"{prefix}{symbol}", period="1", adjust="")
        if df is None or df.empty:
            return out
        df = df.copy()
        df["day"] = df["day"].astype(str)
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
        trade_date = last_date.replace("-", "")
        now = datetime.today()
        is_intraday = (last_date == now.strftime("%Y-%m-%d")) and (as_of_time[11:16] < "15:00")

        # 昨收（真实价）：新浪优先，失败回落日线不复权
        prev_close = _sina_prev_close(ts_code)
        prev_vol_shou: Optional[float] = None
        if not prev_close or True:  # 昨收和昨量都从日线取一次（不复权真实价）
            try:
                bars = json.loads(get_daily_price(ts_code, adj="none"))
                prev_bars = [b for b in bars if str(b.get("trade_date")) != trade_date]
                if prev_bars:
                    if not prev_close:
                        prev_close = float(prev_bars[-1]["close"])
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
            "trade_date": trade_date,
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
    # 妙想 MX API tools
    "mx_data_query": _mx.mx_data_query,
    "mx_news_search": _mx.mx_news_search,
    "mx_stock_screen": _mx.mx_stock_screen,
}
