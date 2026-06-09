"""技术面批量计算模块。

为技术面策略提供共享的日线数据拉取 + 指标缓存。
同一次 screener run 中所有策略共享同一份缓存，避免重复调 API。

使用方式（在策略中）：
  bars_cache = by_source.get("_technical", {})
  bars = bars_cache.get(ts_code)  # list[dict] 升序，含 ma5/ma10/ma20/vol_ratio
  if bars is None or len(bars) < 20:
      continue
"""
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
