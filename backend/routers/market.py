"""行情数据接口：实时/日线价格、分时、个股基本面与市场上下文。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from apex import data

from backend.core.response import parse_json

router = APIRouter(prefix="/market", tags=["market"])


def _split_codes(codes: str) -> list[str]:
    return [c.strip() for c in codes.split(",") if c.strip()]


# ── 价格 ──────────────────────────────────────────────────────────────────────


@router.get("/prices")
def get_prices(codes: str = Query(..., description="逗号分隔的 ts_code")):
    """批量现价：实时优先，盘中无价时回退日线收盘（对齐 Streamlit 缓存逻辑）。"""
    code_list = _split_codes(codes)
    if not code_list:
        return {}
    realtime = data.get_realtime_price(code_list)
    latest = data.get_latest_price(code_list)
    return {
        c: (realtime.get(c) if realtime.get(c) is not None else latest.get(c))
        for c in code_list
    }


@router.get("/prices/realtime")
def get_realtime_prices(codes: str = Query(...)):
    """盘中实时价（新浪）。"""
    return data.get_realtime_price(_split_codes(codes))


@router.get("/prices/daily")
def get_daily_close_prices(codes: str = Query(...)):
    """最新日线收盘价(tushare, 含今天)。

    ⚠️ 不是「昨收」: 盘后 tushare 发了当日个股 daily(约 15:30)后, 本端点返回
    今日收盘, 不能当 prev 基准用(否则今日盈亏 cur==prev 恒为 0)。需要昨收用
    /prices/prev-close。本端点适合「最新可用价」语义(如 analyze 的 current_price)。
    """
    return data.get_latest_price(_split_codes(codes))


@router.get("/prices/prev-close")
def get_prev_close_prices(codes: str = Query(...)):
    """昨收价（前一交易日收盘, 严格排除今天）。

    tushare 个股日线收盘后约 15:30 发布当日数据, /prices/daily 此时返回今日收盘
    而非昨收。本端点取 trade_date < today 的最新收盘, 用于「今日盈亏」的 prev 基准,
    盘中/盘后都正确。
    """
    return data.get_prev_close(_split_codes(codes))


@router.get("/index-daily")
def get_index_daily(
    code: str = Query(..., description="指数代码, 000001.SH / 399001.SZ / 399006.SZ"),
    days: int = Query(2, ge=1, le=30, description="最近 N 天"),
):
    """指数日线(ED13 端点): 用于概览首页市场温度的 vol/成交。

    返回结构: { code, name, bars: [{ trade_date, close, vol, pct_chg, amount }] }
    后端 parse_json 自动解 JSON 字符串。
    """
    return parse_json(data.get_index_daily(code.strip(), days=days))


@router.get("/index-daily/batch")
def get_index_daily_batch(
    codes: str = Query(..., description="逗号分隔指数代码"),
    days: int = Query(2, ge=1, le=30, description="最近 N 天"),
):
    """批量指数日线(ED13): 一次返回多只, 用于市场温度卡。

    返回 { [code]: { code, name, bars: [{ trade_date, close, vol, pct_chg, amount }] } }。
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    return parse_json(data.get_index_daily_batch(code_list, days=days))


@router.get("/index-realtime/batch")
def get_index_realtime_batch(
    codes: str = Query(..., description="逗号分隔指数代码"),
):
    """批量指数实时行情(新浪): 盘中/盘后兜底, 用于市场温度卡在 tushare EOD
    当日数据发布前显示当日点位 + 涨跌幅 + 成交额。

    返回 { [code]: { code, close, prev_close, pct_chg, amount(千元), trade_date } }。
    失败 code 对应空 dict。前端据 trade_date==今天决定是否覆盖 EOD 展示。
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    return parse_json(data.get_index_realtime_batch(code_list))


# ── 分时 ──────────────────────────────────────────────────────────────────────


@router.get("/intraday/{ts_code}/bars")
def get_intraday_bars(ts_code: str):
    """当日分时 K 线 + VWAP 原始数据。"""
    return data.get_intraday_bars(data.normalize_ts_code(ts_code))


@router.get("/intraday/{ts_code}/snapshot")
def get_intraday_snapshot(ts_code: str):
    """分时快照（形态/ swings / 量价匹配等摘要）。"""
    return parse_json(data.get_intraday_snapshot(data.normalize_ts_code(ts_code)))


# ── 个股数据 ──────────────────────────────────────────────────────────────────


@router.get("/stocks/{ts_code}/info")
def get_stock_info(ts_code: str):
    """基本信息（名称/行业/流通市值等）。"""
    return parse_json(data.get_stock_info(ts_code=data.normalize_ts_code(ts_code)))


@router.get("/stocks/{ts_code}/daily")
def get_daily_price(
    ts_code: str,
    start_date: Optional[str] = Query(None, description="YYYYMMDD 或 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None),
    adj: str = Query("qfq", description="qfq/hfq/none"),
):
    """日线 OHLCV + 均线。"""
    return parse_json(data.get_daily_price(
        data.normalize_ts_code(ts_code),
        start_date=start_date, end_date=end_date, adj=adj,
    ))


@router.get("/stocks/{ts_code}/fundamentals")
def get_fundamentals(ts_code: str):
    """基本面（PE/PB/ROE/财务季度/风险标志）。"""
    return parse_json(data.get_fundamentals(data.normalize_ts_code(ts_code)))


@router.get("/stocks/{ts_code}/market-context")
def get_market_context(ts_code: str):
    """大盘/板块/资金面上下文（注入分析用的文本）。"""
    return {"context": data.get_market_context(data.normalize_ts_code(ts_code))}


@router.get("/stocks/{ts_code}/dragon-tiger")
def get_dragon_tiger(ts_code: str, days: int = Query(90, ge=1, le=365)):
    """龙虎榜上榜历史。"""
    return parse_json(data.get_dragon_tiger_list(
        data.normalize_ts_code(ts_code), days=days, fetch_seats=False,
    ))


@router.get("/stocks/{ts_code}/unlock-schedule")
def get_unlock_schedule(ts_code: str, days_ahead: int = Query(180, ge=1, le=730)):
    """限售解禁日程。"""
    return parse_json(data.get_unlock_schedule(
        data.normalize_ts_code(ts_code), days_ahead=days_ahead,
    ))
