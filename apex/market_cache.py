"""行情缓存层：raw 不复权日线 + adj_factor 落盘 parquet，读取时本地复权。

绕开 tushare pro_bar 的双接口（daily + adj_factor）问题:
  - pro.daily + pro.adj_factor 分开调，各自缓存
  - 历史数据落盘，增量拉新，回测反复跑命中率趋近 100% → tushare 请求趋近 0
  - qfq 读取时动态算: price × adj / latest_adj（已实证与 pro_bar(qfq) 一致）
  - 超限自动 sleep 重试

缓存路径:
  ~/.stock-journal/cache/daily/<ts_code>.parquet  (列含 adj_factor)
  ~/.stock-journal/cache/index/<code>.parquet
"""
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from apex import config


# ── 限频兜底 ──────────────────────────────────────────────────────────────────

_RATE_LIMIT_MARKERS = ("频率超限", "抱歉，您访问接口", "rate limit", "Too Many Requests")


def _rate_limited_call(fn, *args, **kwargs):
    """tushare 接口调用包装：识别超限 → 指数退避重试。其他异常直接抛。

    退避 = retry_sleep * 2^attempt + jitter，封顶 30s。tushare 限频是滑动窗口，
    固定 1.5s 重试往往仍在窗口内继续撞限；指数退避让重试拉开足够距离真正滑出窗口。
    """
    cfg = (config.get() or {}).get("cache") or {}
    max_retries = int(cfg.get("max_retries", 4))
    base_sleep = float(cfg.get("retry_sleep", 1.5))
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            msg = str(e)
            if not any(m in msg for m in _RATE_LIMIT_MARKERS):
                raise
            if attempt >= max_retries:
                raise
            # 指数退避 + jitter：2^attempt 倍 base，封顶 30s，再叠 0~1s 抖动避免多线程同步重试
            sleep_s = min(base_sleep * (2 ** attempt), 30.0) + random.random()
            time.sleep(sleep_s)
    raise last_exc  # pragma: no cover


# ── 路径 ──────────────────────────────────────────────────────────────────────

def _cache_dir() -> Path:
    cfg = config.get()
    p = Path((cfg.get("paths") or {}).get("cache_dir", "~/.stock-journal/cache")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_path(kind: str, code: str) -> Path:
    return _cache_dir() / kind / f"{code.replace('/', '_')}.parquet"


def _to_yyyymmdd(d: str) -> str:
    return (d or "").replace("-", "")


def _prev_date(d: str) -> str:
    return (datetime.strptime(d, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")


def _next_date(d: str) -> str:
    return (datetime.strptime(d, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")


# ── 交易日历 ──────────────────────────────────────────────────────────────────

def _trade_cal(start: str, end: str) -> list[str]:
    """返回 [start,end] 内 SSE 开市日列表(YYYYMMDD 升序)。落盘缓存，1 次/范围。"""
    path = _cache_path("index", "trade_cal")
    cached = pd.DataFrame()
    if path.exists():
        try:
            cached = pd.read_parquet(path)
        except Exception:
            cached = pd.DataFrame()
    if not cached.empty:
        cached = cached.sort_values("cal_date").reset_index(drop=True)
        cmin, cmax = cached["cal_date"].iloc[0], cached["cal_date"].iloc[-1]
        if cmin <= start and cmax >= end:
            sub = cached[(cached["cal_date"] >= start) & (cached["cal_date"] <= end)]
            return sub["cal_date"].tolist()
    # 缓存不足 → 拉一次补全（拉一个足够大的窗口，避免反复拉）
    fetch_start = min(start, cached["cal_date"].iloc[0]) if not cached.empty else start
    fetch_end = max(end, cached["cal_date"].iloc[-1]) if not cached.empty else end
    from apex import data
    pro = data._tushare()
    df = _rate_limited_call(
        pro.trade_cal, exchange="SSE", start_date=fetch_start, end_date=fetch_end,
        is_open="1", fields="cal_date",
    )
    if df is None or df.empty:
        # 退化：按日历日粗略返回工作日（保证不阻塞，仅牺牲非交易日空调用）
        return _fallback_calendar(start, end)
    df = df.sort_values("cal_date").reset_index(drop=True)
    if not cached.empty:
        df = pd.concat([cached, df], ignore_index=True).drop_duplicates("cal_date") \
            .sort_values("cal_date").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    sub = df[(df["cal_date"] >= start) & (df["cal_date"] <= end)]
    return sub["cal_date"].tolist()


def _fallback_calendar(start: str, end: str) -> list[str]:
    """trade_cal 拉取失败时的兜底：返回工作日（含少量非交易日，调用方自会收到空数据）。"""
    d0 = datetime.strptime(start, "%Y%m%d")
    d1 = datetime.strptime(end, "%Y%m%d")
    out = []
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


# ── 个股日线: pro.daily + pro.adj_factor ──────────────────────────────────────

def _fetch_daily_raw(ts_code: str, start: str, end: str) -> pd.DataFrame:
    """调 pro.daily + pro.adj_factor 拉一段 raw 不复权日线 + adj_factor。失败抛异常。"""
    from apex import data
    pro = data._tushare()
    df_d = _rate_limited_call(
        pro.daily, ts_code=ts_code, start_date=start, end_date=end,
        fields="ts_code,trade_date,open,high,low,close,vol",
    )
    df_a = _rate_limited_call(
        pro.adj_factor, ts_code=ts_code, start_date=start, end_date=end,
        fields="ts_code,trade_date,adj_factor",
    )
    if df_d is None or df_d.empty:
        return pd.DataFrame()
    df = df_d.sort_values("trade_date").reset_index(drop=True)
    if df_a is not None and not df_a.empty:
        df_a = df_a.sort_values("trade_date").reset_index(drop=True)
        df = df.merge(df_a[["trade_date", "adj_factor"]], on="trade_date", how="left")
        df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce").ffill().bfill()
    else:
        df["adj_factor"] = 1.0
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0)
    df["trade_date"] = df["trade_date"].astype(str)
    return df


def _ensure_daily_range(ts_code: str, start: str, end: str) -> pd.DataFrame:
    """确保缓存覆盖 [start, end]，缺则增量拉，返回该范围 raw+adj DataFrame。

    start/end: YYYYMMDD。前补更早历史、后补更新数据，去重写回 parquet。
    """
    path = _cache_path("daily", ts_code)
    cached = pd.DataFrame()
    if path.exists():
        try:
            cached = pd.read_parquet(path)
        except Exception:
            cached = pd.DataFrame()

    if cached.empty:
        fresh = _fetch_daily_raw(ts_code, start, end)
        if not fresh.empty:
            path.parent.mkdir(parents=True, exist_ok=True)
            fresh.to_parquet(path, index=False)
        if fresh.empty:
            return fresh
        return fresh[(fresh["trade_date"] >= start) & (fresh["trade_date"] <= end)].reset_index(drop=True)

    cached = cached.sort_values("trade_date").reset_index(drop=True)
    cached_min = cached["trade_date"].iloc[0]
    cached_max = cached["trade_date"].iloc[-1]
    fetched = False

    # 前补（更早历史）- 失败 fallback cached，不冒泡（限频/网络抖动不该丢已有数据）
    if start < cached_min:
        try:
            pre = _fetch_daily_raw(ts_code, start, _prev_date(cached_min))
        except Exception as e:
            print(f"⚠ cache 前补 {ts_code} 失败: {type(e).__name__}: {e}，用缓存已有数据")
            pre = pd.DataFrame()
        if not pre.empty:
            cached = pd.concat([pre, cached], ignore_index=True)
            fetched = True
    # 后补（更新数据）- 失败 fallback cached，不冒泡
    if end > cached_max:
        try:
            nxt = _fetch_daily_raw(ts_code, _next_date(cached_max), end)
        except Exception as e:
            print(f"⚠ cache 后补 {ts_code} 失败: {type(e).__name__}: {e}，用缓存已有数据")
            nxt = pd.DataFrame()
        if not nxt.empty:
            cached = pd.concat([cached, nxt], ignore_index=True)
            fetched = True

    if fetched:
        cached = cached.drop_duplicates("trade_date").sort_values("trade_date").reset_index(drop=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        cached.to_parquet(path, index=False)
    else:
        cached = cached.sort_values("trade_date").reset_index(drop=True)

    mask = (cached["trade_date"] >= start) & (cached["trade_date"] <= end)
    return cached[mask].reset_index(drop=True)


def load_raw_daily(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """返回 raw OHLCV + adj_factor（不截断、不复权），index=trade_date(datetime) 升序。

    供需要 raw 数据的场景。失败返空 DataFrame。
    """
    start = _to_yyyymmdd(start_date)
    end = _to_yyyymmdd(end_date)
    try:
        df = _ensure_daily_range(ts_code, start, end)
    except Exception as e:
        print(f"⚠ cache load_raw_daily {ts_code} 失败: {type(e).__name__}: {e}")
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    return df.set_index("trade_date").sort_index()


def _fetch_daily_by_dates(trade_dates: list[str], ts_codes: list[str]) -> pd.DataFrame:
    """按交易日横截面批量拉取（pro.daily(trade_date=D) 返回全市场那天数据），
    过滤到 ts_codes。返回多 code DataFrame: ts_code, trade_date, OHLCV, adj_factor。

    调用次数 = len(trade_dates) × 2 (daily + adj_factor)，与股票数解耦。
    """
    if not trade_dates or not ts_codes:
        return pd.DataFrame()
    from apex import data
    pro = data._tushare()
    codes_set = set(ts_codes)
    frames = []
    for d in trade_dates:
        df_d = _rate_limited_call(
            pro.daily, trade_date=d,
            fields="ts_code,trade_date,open,high,low,close,vol",
        )
        df_a = _rate_limited_call(
            pro.adj_factor, trade_date=d,
            fields="ts_code,trade_date,adj_factor",
        )
        if df_d is None or df_d.empty:
            continue
        df_d = df_d[df_d["ts_code"].isin(codes_set)].copy()
        if df_a is not None and not df_a.empty:
            df_a = df_a[df_a["ts_code"].isin(codes_set)]
            df_d = df_d.merge(df_a[["ts_code", "trade_date", "adj_factor"]],
                              on=["ts_code", "trade_date"], how="left")
        else:
            df_d["adj_factor"] = 1.0
        frames.append(df_d)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    for c in ("open", "high", "low", "close"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["vol"] = pd.to_numeric(out["vol"], errors="coerce").fillna(0)
    out["adj_factor"] = pd.to_numeric(out["adj_factor"], errors="coerce").ffill().bfill()
    out["trade_date"] = out["trade_date"].astype(str)
    return out


def load_raw_daily_batch(ts_codes: list[str], start_date: str, end_date: str) -> dict:
    """批量加载多只股票 raw 日线（screener 场景）。

    按交易日横截面拉取，调用数与股票数解耦：冷启 ~80 交易日 × 2 接口 ≈ 160 次，
    远低于 adj_factor 200/min 限频（旧 per-code 模式 N 只 = N×2 次，250 只即破限）。

    warm code 直接读 parquet；cold code 计算各自缺失日期的并集，按日拉一次横截面
    填充所有 cold code 并落盘。返回 dict {ts_code: DataFrame(index=trade_date, 含 adj_factor)}。
    """
    start = _to_yyyymmdd(start_date)
    end = _to_yyyymmdd(end_date)
    needed = _trade_cal(start, end)
    needed_set = set(needed)

    cached_map: dict = {}
    cold: list[str] = []
    for code in ts_codes:
        path = _cache_path("daily", code)
        df = pd.DataFrame()
        if path.exists():
            try:
                df = pd.read_parquet(path)
            except Exception:
                df = pd.DataFrame()
        have: set = set()
        if not df.empty:
            df = df.sort_values("trade_date").reset_index(drop=True)
            cached_map[code] = df
            have = set(df["trade_date"].astype(str).tolist())
        if not needed_set.issubset(have):
            cold.append(code)

    if cold:
        fetch_dates: set = set()
        for code in cold:
            base = cached_map.get(code)
            have = set(base["trade_date"].astype(str).tolist()) if base is not None and not base.empty else set()
            fetch_dates |= (needed_set - have)
        fetch_dates_sorted = sorted(fetch_dates)
        if fetch_dates_sorted:
            try:
                cross = _fetch_daily_by_dates(fetch_dates_sorted, cold)
            except Exception as e:
                print(f"⚠ cache batch 按日拉取失败({len(fetch_dates_sorted)}日): {type(e).__name__}: {e}")
                cross = pd.DataFrame()
            if not cross.empty:
                for code, grp in cross.groupby("ts_code"):
                    grp = grp.sort_values("trade_date").reset_index(drop=True)
                    base = cached_map.get(code)
                    merged = pd.concat([base, grp], ignore_index=True) \
                        if base is not None and not base.empty else grp
                    merged = merged.drop_duplicates("trade_date") \
                        .sort_values("trade_date").reset_index(drop=True)
                    cached_map[code] = merged
                    path = _cache_path("daily", code)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    merged.to_parquet(path, index=False)

    result = {}
    for code in ts_codes:
        df = cached_map.get(code)
        if df is None or df.empty:
            continue
        mask = (df["trade_date"] >= start) & (df["trade_date"] <= end)
        sub = df[mask].copy()
        if sub.empty:
            continue
        sub["trade_date"] = pd.to_datetime(sub["trade_date"], format="%Y%m%d")
        result[code] = sub.set_index("trade_date").sort_index()
    return result


def _apply_adj(df: pd.DataFrame, adj: str) -> pd.DataFrame:
    """在 raw+adj_factor DataFrame 上做复权。qfq=前复权(以最新 adj 归一)，none=原样。"""
    if adj == "none" or "adj_factor" not in df.columns:
        return df
    if adj == "qfq":
        latest_adj = float(df["adj_factor"].iloc[-1])
        if latest_adj > 0:
            for c in ("open", "high", "low", "close"):
                df[c] = (df[c] * df["adj_factor"] / latest_adj).round(2)
        return df
    raise ValueError(f"adj={adj} 暂不支持，缓存层仅支持 qfq/none（hfq 走上层 fallback）")


def load_daily_full(ts_code: str, start_date: str, end_date: str,
                    adj: str = "qfq") -> pd.DataFrame:
    """返回复权日线全段（不 tail、不 MA），index=trade_date(datetime) 升序。

    供 backtest 长历史（绕开 get_daily_price 的 tail(60)）。失败返空。
    """
    df = load_raw_daily(ts_code, start_date, end_date)
    if df.empty:
        return df
    return _apply_adj(df, adj)


def load_daily(ts_code: str, start_date: Optional[str] = None,
               end_date: Optional[str] = None, adj: str = "qfq") -> pd.DataFrame:
    """返回复权日线 + MA5/10/20/60 + vol_ratio（替代 get_daily_price 内部逻辑）。

    tail(60) 兼容旧契约。adj: qfq(默认)/none。hfq 抛 ValueError 由上层 fallback akshare。
    返回列: trade_date, open, high, low, close, vol, ma5, ma10, ma20, ma60, vol_ratio。
    """
    if not start_date:
        start_date = (datetime.today() - timedelta(days=120)).strftime("%Y%m%d")
    if not end_date:
        end_date = datetime.today().strftime("%Y%m%d")

    df = load_raw_daily(ts_code, start_date, end_date)
    if df.empty:
        return pd.DataFrame()
    df = _apply_adj(df, adj)

    df["ma5"] = df["close"].rolling(5).mean().round(2)
    df["ma10"] = df["close"].rolling(10).mean().round(2)
    df["ma20"] = df["close"].rolling(20).mean().round(2)
    df["ma60"] = df["close"].rolling(60).mean().round(2)
    df["vol_ratio"] = (df["vol"] / df["vol"].rolling(5).mean()).round(2)

    cols = ["open", "high", "low", "close", "vol", "ma5", "ma10", "ma20", "ma60", "vol_ratio"]
    out = df[cols].tail(60).copy()
    out["trade_date"] = out.index.strftime("%Y%m%d")
    return out.reset_index(drop=True)[["trade_date"] + cols]


# ── 指数日线: pro.index_daily ─────────────────────────────────────────────────

def _fetch_index_raw(code: str, start: str, end: str) -> pd.DataFrame:
    from apex import data
    pro = data._tushare()
    df = _rate_limited_call(
        pro.index_daily, ts_code=code, start_date=start, end_date=end,
        fields="ts_code,trade_date,close",
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df["trade_date"] = df["trade_date"].astype(str)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.sort_values("trade_date").reset_index(drop=True)


def load_index_daily(code: str, start_date: str, end_date: str) -> pd.Series:
    """返回指数 close Series，index=trade_date(datetime) 升序。供 benchmark 本地算。失败返空。"""
    start = _to_yyyymmdd(start_date)
    end = _to_yyyymmdd(end_date)
    path = _cache_path("index", code)
    cached = pd.DataFrame()
    if path.exists():
        try:
            cached = pd.read_parquet(path)
        except Exception:
            cached = pd.DataFrame()

    if not cached.empty:
        cached = cached.sort_values("trade_date").reset_index(drop=True)
        cmin, cmax = cached["trade_date"].iloc[0], cached["trade_date"].iloc[-1]
        need = (start < cmin) or (end > cmax)
    else:
        need = True

    if need:
        try:
            fresh = _fetch_index_raw(code, start, end)
        except Exception as e:
            print(f"⚠ cache load_index {code} 失败: {type(e).__name__}: {e}")
            fresh = pd.DataFrame()
        if not fresh.empty:
            if cached.empty:
                cached = fresh
            else:
                cached = pd.concat([cached, fresh], ignore_index=True) \
                    .drop_duplicates("trade_date") \
                    .sort_values("trade_date").reset_index(drop=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            cached.to_parquet(path, index=False)

    if cached.empty:
        return pd.Series(dtype=float)
    cached = cached.sort_values("trade_date").reset_index(drop=True)
    mask = (cached["trade_date"] >= start) & (cached["trade_date"] <= end)
    sub = cached[mask].copy()
    sub["trade_date"] = pd.to_datetime(sub["trade_date"], format="%Y%m%d")
    return sub.set_index("trade_date")["close"].astype(float)
