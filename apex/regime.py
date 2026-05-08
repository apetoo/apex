"""市场温度感知（lite 版）—— 给策略选择器 + screener 的 system prompt 提供上下文。

不做趋势判断（那是 Phase 3 的全量 regime），只采集当日指标 + 三档分类。

输出 ~/.stock-journal/regime/<YYYY-MM-DD>.json，结构：
  {
    "trade_date": "2026-05-07",
    "computed_at": "...",
    "metrics": {
        "limit_up_count": int | null,
        "limit_down_count": int | null,
        "hs300_pct": float | null,
        "csi1000_pct": float | null,
        "style_small_minus_large": float | null,    # 中证1000 - 沪深300
        "northbound_inflow_yi": float | null,       # 北向净流入（亿元）
        "industry_median_pct": float | null,
        "industry_pct_range": float | null,
        "industry_top": [{name, pct}, ...]          # Top 5 行业
    },
    "label": "risk_on" | "neutral" | "risk_off",
    "summary": "<人类可读单行>",
  }

公共 API:
  collect(trade_date, by_source=None) -> dict   # 实时采集 + 落盘
  get(trade_date, force_refresh=False) -> dict  # 读缓存或采集
  load(trade_date) -> dict | None               # 只读缓存
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import config

_TZ_CN = timezone(timedelta(hours=8))


def _path(trade_date: str) -> Path:
    cfg = config.get()
    journal_dir = Path(cfg["paths"]["journal_dir"]).expanduser()
    iso = trade_date if "-" in trade_date else f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    return journal_dir / "regime" / f"{iso}.json"


# ── 指标采集（每个独立容错） ──────────────────────────────────────────────────

def _index_pct(ts_code: str, trade_date: str) -> Optional[float]:
    try:
        from apex import data
        pro = data._tushare()
        df = pro.index_daily(ts_code=ts_code, trade_date=trade_date)
        if df is None or df.empty:
            return None
        return round(float(df.iloc[0]["pct_chg"]), 3)
    except Exception:
        return None


def _northbound_inflow_yi(trade_date: str) -> Optional[float]:
    """北向净流入（亿元）。当日数据可能延迟到收盘后 1-2 小时披露。"""
    try:
        from apex import data
        pro = data._tushare()
        df = pro.moneyflow_hsgt(trade_date=trade_date)
        if df is None or df.empty:
            return None
        # tushare 字段单位：百万元；转亿元
        nb = df.iloc[0].get("north_money")
        if nb is None:
            return None
        return round(float(nb) / 100, 2)
    except Exception:
        return None


def _limit_up_count_from_source(by_source: Optional[dict]) -> Optional[int]:
    """复用已 fetch 的 limit_up 信号池，避免重复调 limit_list_d（限速 1次/小时）。"""
    if not by_source:
        return None
    lu = by_source.get("limit_up")
    if lu is None:
        return None
    return len(lu)


def _industry_stats() -> tuple[Optional[float], Optional[float], list]:
    """行业涨幅中位数、极差、Top 5 行业。akshare 东财接口，需 strip proxy。"""
    try:
        import urllib.request
        import akshare as ak
        import pandas as pd

        # akshare 内部用 requests，无法全局 strip proxy；但中文环境多数能直连
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty or "涨跌幅" not in df.columns:
            return None, None, []
        df["涨跌幅"] = pd.to_numeric(df["涨跌幅"], errors="coerce")
        df = df.dropna(subset=["涨跌幅"])
        if df.empty:
            return None, None, []
        median = round(float(df["涨跌幅"].median()), 2)
        rng = round(float(df["涨跌幅"].max() - df["涨跌幅"].min()), 2)
        top = (
            df.sort_values("涨跌幅", ascending=False).head(5)
            [["板块名称", "涨跌幅"]].to_dict("records")
        )
        top_clean = [{"name": r.get("板块名称", ""), "pct": round(float(r["涨跌幅"]), 2)} for r in top]
        return median, rng, top_clean
    except Exception:
        return None, None, []


# ── 分类 ─────────────────────────────────────────────────────────────────────

def _classify(metrics: dict) -> str:
    """三档分类：risk_on / neutral / risk_off。基于多指标加权积分。"""
    score = 0
    weight = 0

    lu = metrics.get("limit_up_count")
    ld = metrics.get("limit_down_count")
    if lu is not None and ld is not None and (lu + ld) > 0:
        ratio = lu / max(ld, 1)
        if ratio > 5:
            score += 2; weight += 2
        elif ratio > 2:
            score += 1; weight += 2
        elif ratio < 0.5:
            score -= 2; weight += 2
        elif ratio < 1:
            score -= 1; weight += 2
        else:
            weight += 2
    elif lu is not None:
        if lu > 50:
            score += 1; weight += 1
        elif lu < 15:
            score -= 1; weight += 1

    hs300 = metrics.get("hs300_pct")
    if hs300 is not None:
        if hs300 > 1.5:
            score += 2; weight += 2
        elif hs300 > 0.3:
            score += 1; weight += 2
        elif hs300 < -1.5:
            score -= 2; weight += 2
        elif hs300 < -0.3:
            score -= 1; weight += 2
        else:
            weight += 2

    nb = metrics.get("northbound_inflow_yi")
    if nb is not None:
        if nb > 30:
            score += 1; weight += 1
        elif nb < -30:
            score -= 1; weight += 1

    ind_median = metrics.get("industry_median_pct")
    if ind_median is not None:
        if ind_median > 0.8:
            score += 1; weight += 1
        elif ind_median < -0.8:
            score -= 1; weight += 1

    if weight == 0:
        return "neutral"

    avg = score / weight
    if avg >= 0.5:
        return "risk_on"
    if avg <= -0.5:
        return "risk_off"
    return "neutral"


def _make_summary(m: dict, label: str) -> str:
    parts = []
    lu, ld = m.get("limit_up_count"), m.get("limit_down_count")
    if lu is not None and ld is not None:
        parts.append(f"涨停 {lu}/跌停 {ld}")
    elif lu is not None:
        parts.append(f"涨停 {lu}")

    hs300, csi1000 = m.get("hs300_pct"), m.get("csi1000_pct")
    if hs300 is not None:
        parts.append(f"沪深300 {hs300:+.2f}%")
    if csi1000 is not None:
        parts.append(f"中证1000 {csi1000:+.2f}%")

    style = m.get("style_small_minus_large")
    if style is not None:
        if style > 0.3:
            parts.append(f"小盘强 ({style:+.2f})")
        elif style < -0.3:
            parts.append(f"大盘强 ({style:+.2f})")
        else:
            parts.append(f"风格均衡 ({style:+.2f})")

    nb = m.get("northbound_inflow_yi")
    if nb is not None:
        parts.append(f"北向 {nb:+.0f}亿")

    ind_med = m.get("industry_median_pct")
    if ind_med is not None:
        parts.append(f"行业中位 {ind_med:+.2f}%")

    if not parts:
        return f"数据不足 → {label}"
    return f"{' ｜ '.join(parts)} → {label}"


# ── 入口 ─────────────────────────────────────────────────────────────────────

def collect(trade_date: str, by_source: Optional[dict] = None) -> dict:
    """实时采集 regime（不读缓存）。落盘后返回。"""
    td = trade_date.replace("-", "")

    metrics: dict = {}
    metrics["limit_up_count"] = _limit_up_count_from_source(by_source)
    metrics["limit_down_count"] = None  # 限速接口，先不拉
    metrics["hs300_pct"] = _index_pct("000300.SH", td)
    metrics["csi1000_pct"] = _index_pct("000852.SH", td)
    if metrics["csi1000_pct"] is not None and metrics["hs300_pct"] is not None:
        metrics["style_small_minus_large"] = round(
            metrics["csi1000_pct"] - metrics["hs300_pct"], 2
        )
    else:
        metrics["style_small_minus_large"] = None
    metrics["northbound_inflow_yi"] = _northbound_inflow_yi(td)

    ind_median, ind_range, ind_top = _industry_stats()
    metrics["industry_median_pct"] = ind_median
    metrics["industry_pct_range"] = ind_range
    metrics["industry_top"] = ind_top

    label = _classify(metrics)
    summary = _make_summary(metrics, label)

    iso = f"{td[:4]}-{td[4:6]}-{td[6:]}"
    out = {
        "trade_date": iso,
        "computed_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
        "metrics": metrics,
        "label": label,
        "summary": summary,
    }

    p = _path(iso)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out


def load(trade_date: str) -> Optional[dict]:
    """只读缓存。"""
    p = _path(trade_date)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def get(trade_date: str, by_source: Optional[dict] = None,
        force_refresh: bool = False) -> dict:
    """读缓存，缺失或 force_refresh 时实时采集。"""
    if not force_refresh:
        cached = load(trade_date)
        if cached:
            return cached
    return collect(trade_date, by_source=by_source)


def format_for_prompt(regime: Optional[dict]) -> str:
    """格式化为 markdown 段落，可塞进 system prompt。"""
    if not regime:
        return "（无 regime 数据）"
    label = regime.get("label", "?")
    summary = regime.get("summary", "")
    metrics = regime.get("metrics", {})
    top = metrics.get("industry_top") or []
    lines = [f"**当前 regime**: `{label}`", summary]
    if top:
        names = "、".join(f"{t['name']}({t['pct']:+.2f}%)" for t in top[:5])
        lines.append(f"**今日 Top 5 行业**: {names}")
    return "\n".join(lines)
