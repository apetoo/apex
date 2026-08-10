"""技术面批量计算模块。

为技术面策略提供共享的日线数据拉取 + 指标缓存。
同一次 screener run 中所有策略共享同一份缓存，避免重复调 API。

使用方式（在策略中）：
  bars_cache = by_source.get("_technical", {})
  bars = bars_cache.get(ts_code)  # list[dict] 升序，含 ma5/ma10/ma20/vol_ratio
  if bars is None or len(bars) < 20:
      continue
"""
from datetime import datetime
from typing import Optional

import pandas as pd

from apex import data

_BARS_CACHE: dict[str, list[dict]] = {}


def clear_cache() -> None:
    _BARS_CACHE.clear()


def _compute_bars(df: pd.DataFrame, ts_code: str, min_bars: int = 20) -> Optional[list[dict]]:
    """Compute MA/vol_ratio on a per-stock DataFrame, return bars list sorted by trade_date."""
    if df is None or df.empty:
        return None
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["ma5"] = df["close"].rolling(5, min_periods=1).mean().round(2)
    df["ma10"] = df["close"].rolling(10, min_periods=1).mean().round(2)
    df["ma20"] = df["close"].rolling(20, min_periods=1).mean().round(2)
    df["ma60"] = df["close"].rolling(60, min_periods=1).mean().round(2)
    df["vol_ratio"] = (df["vol"] / df["vol"].rolling(5, min_periods=1).mean()).round(2)
    bars = df.tail(60).to_dict(orient="records")
    return bars if len(bars) >= min_bars else None


def _raw_daily(ts_codes: list[str], lookback_days: int) -> pd.DataFrame:
    """Batch fetch raw daily bars via tushare pro.daily (no adj_factor)."""
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=lookback_days + 10)).strftime("%Y%m%d")
    pro = data._tushare()
    codes_str = ",".join(ts_codes)
    df = pro.daily(ts_code=codes_str, start_date=start, end_date=end,
                   fields="ts_code,trade_date,open,high,low,close,vol")
    if df is None or df.empty:
        return pd.DataFrame()
    df["trade_date"] = df["trade_date"].astype(str)
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0)
    return df


def fetch_bars(ts_code: str, lookback_days: int = 120) -> Optional[list[dict]]:
    """Fetch daily bars for a single stock. 直接调 pro.daily（不触 adj_factor 接口）。"""
    if ts_code in _BARS_CACHE:
        return _BARS_CACHE[ts_code]
    try:
        raw_df = _raw_daily([ts_code], lookback_days)
        bars = _compute_bars(raw_df, ts_code)
        _BARS_CACHE[ts_code] = bars
        return bars
    except Exception:
        _BARS_CACHE[ts_code] = None
        return None


def batch_fetch_bars(ts_codes: list[str], lookback_days: int = 120) -> dict[str, list[dict]]:
    """Batch fetch daily bars for many stocks. 全部用 pro.daily 批量接口，不触 adj_factor。

    限流应对：单次调用 pro.daily 传逗号分隔最多 500 只股票。
    超过则分批拉取。tushare daily API 限流 200次/分钟，批量接口一次=1次配额。
    """
    needed = [c for c in ts_codes if c not in _BARS_CACHE]
    if not needed:
        return {c: _BARS_CACHE[c] for c in ts_codes if _BARS_CACHE.get(c) is not None}

    # 分批：pro.daily 的 ts_code 参数不宜超过 500 只（URL 长度 + tushare 服务端限制）
    batch_size = 400
    for i in range(0, len(needed), batch_size):
        batch = needed[i:i + batch_size]
        try:
            raw_df = _raw_daily(batch, lookback_days)
            for ts_code, grp in raw_df.groupby("ts_code"):
                bars = _compute_bars(grp, ts_code)
                _BARS_CACHE[ts_code] = bars
        except Exception:
            for ts_code in batch:
                if ts_code not in _BARS_CACHE:
                    _BARS_CACHE[ts_code] = None

    return {c: _BARS_CACHE.get(c) for c in ts_codes if _BARS_CACHE.get(c) is not None}


# ── 缠论页原始 K 线（不复权，绕过 _BARS_CACHE） ─────────────────────────────


class DataFetchError(Exception):
    """所有数据源都失败。backend 映射 502（对齐 AnalysisError 先例）。"""


def _completed_minute_bars(
    bars: list[dict], now: Optional[datetime] = None
) -> list[dict]:
    """剔除尚未收完的东财分钟 K。

    push2his 的分钟 `dt` 是该根 K 线的区间结束时刻；只有 `dt <= now`
    才能进入缠论计算，否则盘中结构会被未完成 bar 重画。
    """
    cutoff = now or datetime.now()
    return [bar for bar in bars if bar["dt"] <= cutoff]


def _df_to_bars(df: pd.DataFrame, n: int) -> list[dict]:
    """pro.daily/pro.fund_daily/pro.weekly 的 DataFrame → 升序 bars list[dict]。"""
    df = df.sort_values("trade_date").reset_index(drop=True)
    out = []
    for r in df.tail(n).itertuples():
        out.append({
            "dt": pd.to_datetime(r.trade_date).to_pydatetime(),
            "open": float(r.open), "high": float(r.high),
            "low": float(r.low), "close": float(r.close),
            "vol": float(r.vol), "amount": float(getattr(r, "amount", 0) or 0),
        })
    return out


def _tushare_daily_bars(ts_code: str, n: int, freq: str) -> Optional[list[dict]]:
    """tushare 日/周线。ETF 走 fund_daily（pro.daily 对 ETF 恒 0 行，实测）。"""
    from datetime import datetime, timedelta
    days = n * (7 if freq == "W" else 2) + 30
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    pro = data._tushare()
    fields = "ts_code,trade_date,open,high,low,close,vol,amount"
    if freq == "W" and not data.is_etf(ts_code):
        df = pro.weekly(ts_code=ts_code, start_date=start, end_date=end, fields=fields)
        return None if df is None or df.empty else _df_to_bars(df, n)
    if data.is_etf(ts_code):
        df = pro.fund_daily(ts_code=ts_code, start_date=start, end_date=end, fields=fields)
    else:
        df = pro.daily(ts_code=ts_code, start_date=start, end_date=end, fields=fields)
    if df is None or df.empty:
        return None
    bars = _df_to_bars(df, n * 5 if freq == "W" else n)
    if freq == "W":
        bars = _resample_weekly(bars, n)
    return bars


def _resample_weekly(bars: list[dict], n: int) -> list[dict]:
    """日 bars 重采样为周 bars（ETF 周线：tushare 无 fund_weekly）。"""
    if not bars:
        return bars
    df = pd.DataFrame(bars).set_index("dt")
    agg = df.resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last",
         "vol": "sum", "amount": "sum"}).dropna(subset=["close"])
    out = [{"dt": idx.to_pydatetime(), **{k: float(v) for k, v in row.items()}}
           for idx, row in agg.iterrows()]
    return out[-n:]


def _akshare_daily_bars(ts_code: str, n: int) -> Optional[list[dict]]:
    """akshare 东财日线兜底（个股/ETF）。东财系接口无 V8，不需 _AKSHARE_LOCK。"""
    import akshare as ak
    from datetime import datetime, timedelta
    start = (datetime.now() - timedelta(days=n * 2 + 30)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    code6 = ts_code.split(".")[0]
    if data.is_etf(ts_code):
        df = ak.fund_etf_hist_em(symbol=code6, period="daily",
                                 start_date=start, end_date=end, adjust="")
    else:
        df = ak.stock_zh_a_hist(symbol=code6, period="daily",
                                start_date=start, end_date=end, adjust="")
    if df is None or df.empty:
        return None
    df = df.rename(columns={"日期": "dt", "开盘": "open", "最高": "high",
                            "最低": "low", "收盘": "close", "成交量": "vol",
                            "成交额": "amount"})
    out = []
    for r in df.tail(n).itertuples():
        out.append({"dt": pd.to_datetime(r.dt).to_pydatetime(),
                    "open": float(r.open), "high": float(r.high),
                    "low": float(r.low), "close": float(r.close),
                    "vol": float(r.vol), "amount": float(getattr(r, "amount", 0) or 0)})
    return out


def fetch_raw_bars(ts_code: str, n: int = 250, freq: str = "D") -> list[dict]:
    """缠论页原始 K 线：不复权、**绕过 _BARS_CACHE**（60/250 根缓存键互污染前科）。

    freq: "D"（日）/ "W"（周）/ "30" / "60"（分钟）。
    返回 list[dict] 升序：{dt, open, high, low, close, vol, amount}。

    路由（day-0 实测定案）：
      日/周  个股+BJ(920)→pro.daily/pro.weekly；ETF→pro.fund_daily（pro.daily 对 ETF 恒空）
             日线兜底 akshare（东财系）；分钟无 tushare 源（stk_mins 限 2 次/天）
      分钟   东财 push2his klt=30/60（页面级低频，封 IP 前科，禁止批量）
             BJ 分钟不支持（东财 920 secid 未验证）→ DataFetchError
      全挂   → DataFetchError（backend 502）
    """
    ts_code = data.normalize_ts_code(ts_code)
    if freq in ("30", "60"):
        if ts_code.endswith(".BJ"):
            raise DataFetchError("BJ 分钟数据暂不可用")
        bars = data.minute_kline(ts_code, freq=freq, n=n)
        if bars:
            completed = _completed_minute_bars(bars)
            if completed:
                return completed
        raise DataFetchError(f"{ts_code} {freq}min 分钟数据源失败")

    errors = []
    try:
        bars = _tushare_daily_bars(ts_code, n, freq)
        if bars:
            return bars
        errors.append("tushare empty")
    except Exception as e:
        errors.append(f"tushare: {e}")
    if freq == "D":
        try:
            bars = _akshare_daily_bars(ts_code, n)
            if bars:
                return bars
            errors.append("akshare empty")
        except Exception as e:
            errors.append(f"akshare: {e}")
    raise DataFetchError(f"{ts_code} {freq} 数据源全部失败: {'; '.join(errors)}")


# ── 指标计算（输入为 bars list[dict] 升序） ──────────────────────────────────


def latest(bars: list[dict]) -> Optional[dict]:
    return bars[-1] if bars else None


def ma_distance(bars: list[dict], period: int = 5) -> Optional[float]:
    """最新价离 MA 的距离百分比。正 = 价在 MA 上方。"""
    latest_row = latest(bars)
    if not latest_row:
        return None
    try:
        close = float(latest_row["close"])
        ma = float(latest_row.get(f"ma{period}", 0) or 0)
    except (TypeError, ValueError):
        return None
    if ma <= 0:
        return None
    return round((close - ma) / ma * 100, 2)


def is_near_ma(bars: list[dict], period: int = 5, threshold_pct: float = 3.0) -> bool:
    """价格在 MA 上下 threshold% 之内？"""
    d = ma_distance(bars, period)
    if d is None:
        return False
    return abs(d) <= threshold_pct


def volume_ratio(bars: list[dict]) -> Optional[float]:
    """今日量比（今日 vol / 5日均量）。"""
    latest_row = latest(bars)
    if not latest_row:
        return None
    try:
        return float(latest_row.get("vol_ratio", 0) or 0)
    except (TypeError, ValueError):
        return None


def is_volume_expanding(bars: list[dict], min_ratio: float = 1.5) -> bool:
    """量比大于阈值？"""
    vr = volume_ratio(bars)
    return vr is not None and vr >= min_ratio


def is_volume_shrinking(bars: list[dict], max_ratio: float = 1.1) -> bool:
    """量比小于阈值（缩量调整）。"""
    vr = volume_ratio(bars)
    return vr is not None and vr <= max_ratio


def check_alignment(bars: list[dict]) -> Optional[str]:
    """均线排列。返回 'bullish' / 'bearish' / 'mixed' / None。"""
    latest_row = latest(bars)
    if not latest_row:
        return None
    try:
        ma5 = float(latest_row.get("ma5", 0) or 0)
        ma10 = float(latest_row.get("ma10", 0) or 0)
        ma20 = float(latest_row.get("ma20", 0) or 0)
    except (TypeError, ValueError):
        return None
    if ma5 <= 0 or ma10 <= 0 or ma20 <= 0:
        return None
    if ma5 > ma10 > ma20:
        return "bullish"
    if ma5 < ma10 < ma20:
        return "bearish"
    return "mixed"


def high_distance(bars: list[dict], window: int = 20) -> Optional[float]:
    """最新价离 N 日最高价的百分比。0 = 就是最高价。"""
    if len(bars) < window:
        return None
    try:
        window_bars = bars[-window:]
        high = max(float(b["close"]) for b in window_bars)
        close = float(bars[-1]["close"])
    except (TypeError, ValueError):
        return None
    if high <= 0:
        return None
    return round((high - close) / high * 100, 2)


def volume_trend(bars: list[dict], short: int = 5, long: int = 20) -> Optional[float]:
    """量能趋势：短期均量 / 长期均量。>1 = 短期放量。"""
    if len(bars) < long:
        return None
    try:
        short_vols = [float(b.get("vol", 0) or 0) for b in bars[-short:]]
        long_vols = [float(b.get("vol", 0) or 0) for b in bars[-long:]]
    except (TypeError, ValueError):
        return None
    short_avg = sum(short_vols) / len(short_vols) if short_vols else 0
    long_avg = sum(long_vols) / len(long_vols) if long_vols else 0
    if long_avg <= 0:
        return None
    return round(short_avg / long_avg, 2)


def price_vs_ma20(bars: list[dict]) -> Optional[float]:
    """价格相对 MA20 位置百分比。正 = 在 MA20 上方。"""
    return ma_distance(bars, period=20)


def atr_14(bars: list[dict]) -> Optional[float]:
    """14 周期 ATR（Average True Range）。返回绝对值。"""
    if len(bars) < 15:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        try:
            h = float(bars[i]["high"])
            l = float(bars[i]["low"])
            pc = float(bars[i - 1]["close"])
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        except (TypeError, ValueError, KeyError):
            continue
    if len(trs) < 14:
        return None
    return round(sum(trs[-14:]) / 14, 2)


def atr_14_pct(bars: list[dict]) -> Optional[float]:
    """ATR(14) 占最新收盘价的百分比。"""
    atr = atr_14(bars)
    if atr is None:
        return None
    latest_row = bars[-1]
    try:
        close = float(latest_row["close"])
    except (TypeError, ValueError, KeyError):
        return None
    if close <= 0:
        return None
    return round(atr / close * 100, 2)


def realized_vol(bars: list[dict], window: int = 20) -> Optional[float]:
    """近 window 根日 K 的日收益率标准差（小数，不年化）。

    估算"价格靠噪声漂动一个交易日的典型幅度"。不年化因为下游 t* 是日度口径
    （年化会放大 √252 倍, 与 t*=(d/σ)² 的日度语义不符）。
    bars: get_daily_price 解析后的 list[dict], 需含 close, 升序。
    不足 window+1 根返回 None。
    """
    if len(bars) < window + 1:
        return None
    try:
        closes = pd.Series([float(b["close"]) for b in bars[-(window + 1):]])
    except (TypeError, ValueError, KeyError):
        return None
    rets = closes.pct_change().dropna()
    sigma = rets.std()
    if sigma is None or not (sigma > 0):  # None / NaN / 0
        return None
    return round(float(sigma), 6)
