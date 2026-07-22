"""Crowd Behavior 先验层 (Step 0.5 / T1): 千股千评数据采集。

用 akshare 的东方财富"千股千评"免费结构化数据, 验证 crowd 信号 (综合得分/关注指数/
机构参与度/市场参与意愿) 是否预测次日反转。2 周 pre-log 后由 scripts/crowd_validate.py
score 决定 go/no-go 是否建股吧 text 爬虫 (T2-T8)。

设计要点 (见 design doc `wanmingyu-v9-design-20260718-155154.md` Step 0.5):
- **薄: 只采不判**。预测规则 + 命中率计算在 scripts/crowd_validate.py, 此处只 fetch+store。
- **全市场 + 持仓集双层**。stock_comment_em() 一次拿全市场 (综合得分/关注指数/机构参与度
  + 当日涨跌幅, ~5000 票) -> cross-sectional contrarian 大样本; stock_comment_detail_scrd_desire_em()
  per-stock 拿"市场参与意愿"(retail desire, 仅 active_positions+candidates) -> 最 crowd-behavior 信号。
- **裸码**。akshare per-stock 函数吃裸码 (600000), 系统内统一后缀 (600000.SH), 此处剥离。
- **akshare 锁**。复用 apex.data._AKSHARE_LOCK 序列化 (py_mini_racer V8 并发崩溃防护;
  stock_comment_em 虽是纯 HTTP, 保守复用同一锁与 data.py 一致)。
- **存储 append-only**: ~/.stock-crowd/qqp/snapshots/{trade_date}.json, 一日一文件, 不重写历史。
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from apex import config, data

# 复用 data.py 的 akshare 序列化锁 (py_mini_racer V8 并发崩溃防护)。
_AKSHARE_LOCK = data._AKSHARE_LOCK


def _cache_dir() -> Path:
    """crowd 缓存根目录 (默认 ~/.stock-crowd, 可被 config.yaml crowd.cache_dir 覆盖)。"""
    cfg = config.get() or {}
    crowd_cfg = cfg.get("crowd") or {}
    return Path(str(crowd_cfg.get("cache_dir", "~/.stock-crowd"))).expanduser()


def _snapshots_dir() -> Path:
    return _cache_dir() / "qqp" / "snapshots"


def _bare(ts_code: str) -> str:
    """600000.SH -> 600000 (akshare per-stock 函数吃裸码)。"""
    return (ts_code or "").split(".")[0]


def _clean(v):
    """pandas NaN / numpy 标量 / date-like -> JSON-safe (None / Python 原生 / YYYY-MM-DD)。

    date-like (Timestamp / numpy datetime64) 归一成 "YYYY-MM-DD" 字符串, 保证 trade_date
    与 desire.date 同型可比 (否则 date 对象 vs str 永远 !=, 1 日对齐校验全失效)。
    """
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "strftime"):  # Timestamp / datetime / date
        try:
            return v.strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass
    if hasattr(v, "item"):  # numpy datetime64 -> datetime.date / numpy 标量 -> Python
        try:
            iv = v.item()
            if hasattr(iv, "strftime"):
                return iv.strftime("%Y-%m-%d")
            return iv
        except (TypeError, ValueError):
            pass
    return v


def get_population() -> list[str]:
    """active_positions + candidates 的 ts_code (suffixed, 去重保序)。

    Step 0.5 只验持仓集的 desire (per-stock 调用贵), 全市场信号不受此限。
    """
    from apex import watchlist
    wl = watchlist.load()
    codes = [p.get("ts_code") for p in wl.get("active_positions", [])]
    codes += [c.get("ts_code") for c in wl.get("candidates", [])]
    seen, out = set(), []
    for c in codes:
        if not c:
            continue
        tc = data.normalize_ts_code(c)
        if tc not in seen:
            seen.add(tc)
            out.append(tc)
    return out


def fetch_market_panel() -> list[dict]:
    """stock_comment_em() 全市场千股千评 -> 标准化 dict 列表 (suffixed ts_code)。

    含当日涨跌幅 + 综合得分 + 关注指数 + 机构参与度, 是 cross-sectional contrarian
    的大样本面板 (一次调用 ~5000 票)。列名中文化 -> 英文 snake_case 便于下游规则匹配。
    """
    import akshare as ak
    with _AKSHARE_LOCK:
        df = ak.stock_comment_em()
    if df is None or df.empty:
        return []
    col_map = {
        "代码": "ts_code", "名称": "name", "最新价": "close", "涨跌幅": "pct_chg",
        "换手率": "turnover", "市盈率": "pe", "主力成本": "main_cost",
        "机构参与度": "inst_participation", "综合得分": "composite_score",
        "上升": "rank_change", "目前排名": "rank", "关注指数": "focus_index",
        "交易日": "trade_date",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    keep = [v for v in col_map.values() if v in df.columns]
    df = df[keep]
    df["ts_code"] = df["ts_code"].astype(str).map(data.normalize_ts_code)
    return [{k: _clean(v) for k, v in rec.items()} for rec in df.to_dict(orient="records")]


def fetch_desire(ts_code: str) -> list[dict]:
    """stock_comment_detail_scrd_desire_em(symbol) -> 市场参与意愿 (近 5 日, 升序)。

    最 crowd-behavior 的信号: 高参与意愿 = 散户想买 (contrarian 假设: 见顶 -> 次日反转跌)。
    返回 [{date, ts_code, desire, desire_ma5, desire_chg, desire_chg_ma5}, ...]。
    """
    import akshare as ak
    with _AKSHARE_LOCK:
        df = ak.stock_comment_detail_scrd_desire_em(symbol=_bare(ts_code))
    if df is None or df.empty:
        return []
    col_map = {
        "交易日期": "date", "股票代码": "code", "参与意愿": "desire",
        "5日平均参与意愿": "desire_ma5", "参与意愿变化": "desire_chg",
        "5日平均变化": "desire_chg_ma5",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    df["ts_code"] = data.normalize_ts_code(ts_code)
    keep = ["date", "ts_code", "desire", "desire_ma5", "desire_chg", "desire_chg_ma5"]
    keep = [c for c in keep if c in df.columns]
    return [{k: _clean(v) for k, v in rec.items()} for rec in df[keep].to_dict(orient="records")]


def collect(trade_date: Optional[str] = None,
            population: Optional[list[str]] = None) -> dict:
    """采集一日快照: 全市场面板 + 持仓集 per-stock 参与意愿。落盘 append-only (同日可重采)。

    返回快照 dict (同时写 ~/.stock-crowd/qqp/snapshots/{trade_date}.json)。
    trade_date 默认取市场面板自带的"交易日"(东财已给当日交易日); 显式传则覆盖。
    单股 desire 失败不致命 -> 存 {"error": ...}, 其余继续 (partial OK)。
    """
    market = fetch_market_panel()
    if not market:
        raise RuntimeError("stock_comment_em 返回空, 采集失败 (限频/接口变更?)")

    resolved = trade_date
    if not resolved:
        for rec in market:
            if rec.get("trade_date"):
                resolved = rec["trade_date"]
                break
    if not resolved:
        resolved = datetime.now().strftime("%Y-%m-%d")

    if population is None:
        population = get_population()
    desire: dict[str, list] = {}
    for tc in population:
        try:
            desire[tc] = fetch_desire(tc)
        except Exception as e:  # 单股失败不阻断整体采集
            desire[tc] = [{"error": str(e)}]

    snap = {
        "trade_date": resolved,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "source": "akshare stock_comment_em + stock_comment_detail_scrd_desire_em",
        "n_market": len(market),
        "n_population": len(population),
        "market": market,
        "desire": desire,
    }
    _save_snapshot(snap)
    return snap


def _save_snapshot(snap: dict) -> Path:
    d = _snapshots_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{snap['trade_date']}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, default=str)
    return p


def load_snapshot(trade_date: str) -> Optional[dict]:
    p = _snapshots_dir() / f"{trade_date}.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def list_snapshots() -> list[str]:
    """已采集的交易日列表 (升序, YYYY-MM-DD)。"""
    d = _snapshots_dir()
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json"))
