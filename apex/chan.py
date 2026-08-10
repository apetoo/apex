"""缠论结构计算：czsc 分段（笔/中枢）+ 库内买卖点信号，供 /chan 页使用。

pipeline:
  fetch_raw_bars (technical.py, 不复权)
    → czsc.CZSC 逐根 update（增量，与批量结果一致，day-0 实测 220 根 2ms）
      → 每完成一根新笔 → 在该时刻评估 4 个 BSP 信号函数（一/二/三买卖点）
    → get_zs_seq(bi_list) → 中枢序列
      → confirmed-zs 启发式：czsc 构造上 zs_seq[-1] 恒延伸中、之前恒已完结
        （开新中枢的条件 = 一根笔完全离开老区间），故已确认中枢 = zs_seq[:-1] 最后一条
    → 机械跳空检测（除权除息）：|open/prev_close-1| > 板限+1% 且前日非涨跌停
      → ex_div_gap 标记；zs_break 只用完全形成于最近一次跳空之后的中枢
        （除权前中枢 zg/zd 与除权后现价不可比，day-0 实测幅度失真 ~22pp）
    → 序列化 {bars, bi_list, zs_list, bsp_list, summary}

口径披露（前端必须展示）：
  - 买卖点为"本级别近似，未经次级别确认"（纯单级别 BSP 是理论近似）
  - 最后一根笔未完成（confirmed=False），会随行情重画（repaint 是缠论固有属性）
"""
from datetime import datetime
from typing import Optional

from apex import data
from apex.technical import fetch_raw_bars

try:
    from czsc import CZSC, RawBar, Freq, Direction
    from czsc.utils.sig import get_zs_seq
    from czsc.signals.cxt import (
        cxt_first_buy_V221126,
        cxt_first_sell_V221126,
        cxt_second_bs_V240524,
        cxt_third_bs_V230319,
    )
    _CZSC_AVAILABLE = True
except ImportError:  # czsc 未安装 → ChanUnavailableError (backend 501)
    _CZSC_AVAILABLE = False


class ChanUnavailableError(Exception):
    """czsc 不可用（未安装/导入失败）。backend 映射 501。"""


_FREQ_MAP = {"D": "D", "W": "W", "30": "F30", "60": "F60"} if _CZSC_AVAILABLE else {}


def _fmt_dt(dt, freq: str) -> str:
    """日/周线只出日期（czsc 内部把日线 fx 时间规范到 16:00，与 bars 的 00:00 不一致，
    前端 lightweight-charts 按 business day 对齐，必须抹平）；分钟保留到分。"""
    return dt.strftime("%Y-%m-%d %H:%M") if freq in ("30", "60") else dt.strftime("%Y-%m-%d")

# 各板单日涨跌停幅度（机械跳空阈值 = 板限 + 1%）
def _board_limit_pct(ts_code: str) -> float:
    code = ts_code.split(".")[0]
    if code.startswith(("4", "8", "9")):
        return 30.0  # 北交所
    if code.startswith(("30", "68")):
        return 20.0  # 创业板/科创板
    return 10.0      # 主板/ETF（ST 5% 不特判，宁可漏报）


_BSP_SIGNALS = ["一买", "一卖", "二买", "二卖", "三买", "三卖"]
_BSP_TYPE_MAP = {"一买": "1buy", "一卖": "1sell", "二买": "2buy",
                 "二卖": "2sell", "三买": "3buy", "三卖": "3sell"}
_BSP_BUY_TYPES = {"1buy", "2buy", "3buy"}
_BSP_SELL_TYPES = {"1sell", "2sell", "3sell"}
_BSP_LABELS = {value: key for key, value in _BSP_TYPE_MAP.items()}


def _empty_decision() -> dict:
    """Return the stable no-action decision shape used by every response."""
    return {
        "bias": "neutral",
        "setup": "none",
        "state": "watching",
        "bsp_type": None,
        "signal_dt": None,
        "bars_since_signal": None,
        "confirm_price": None,
        "invalidation_price": None,
        "trigger_price": None,
        "trigger_low": None,
        "trigger_high": None,
        "candidate_eligible": False,
        "ineligible_reason": "no_actionable_structure",
        "basis": ["暂无可执行的做多结构"],
    }


def _decision_with(**changes) -> dict:
    decision = _empty_decision()
    decision.update(changes)
    return decision


def _find_bsp_bar(bars: list[dict], bsp: dict, freq: str) -> tuple[Optional[int], Optional[dict]]:
    """Locate a serialized BSP on its completed source bar."""
    for index, bar in enumerate(bars):
        if _fmt_dt(bar["dt"], freq) == bsp.get("dt"):
            return index, bar
    return None, None


def _build_buy_decision(bars: list[dict], bsp: dict, freq: str) -> dict:
    signal_idx, signal_bar = _find_bsp_bar(bars, bsp, freq)
    if signal_bar is None:
        return _decision_with(ineligible_reason="signal_bar_missing")

    try:
        confirmation = round(float(signal_bar["high"]), 3)
        invalidation = round(float(bsp["price"]), 3)
        current_close = float(bars[-1]["close"])
    except (KeyError, TypeError, ValueError):
        return _decision_with(ineligible_reason="no_actionable_structure")

    bars_since_signal = len(bars) - 1 - signal_idx
    confirmed = any(float(bar["close"]) > confirmation
                    for bar in bars[signal_idx + 1:])
    is_invalid = current_close < invalidation
    state = "invalid" if is_invalid else "confirmed" if confirmed else "pending"
    eligible = state != "invalid" and bars_since_signal <= 10
    label = _BSP_LABELS.get(bsp["type"], bsp["type"])
    if state == "invalid":
        reason = "signal_invalidated"
    elif bars_since_signal > 10:
        reason = "stale_signal"
    else:
        reason = None
    return _decision_with(
        bias="risk" if is_invalid else "long",
        setup="bsp_buy",
        state=state,
        bsp_type=bsp["type"],
        signal_dt=bsp["dt"],
        bars_since_signal=bars_since_signal,
        confirm_price=confirmation,
        invalidation_price=invalidation,
        trigger_price=confirmation,
        candidate_eligible=eligible,
        ineligible_reason=reason,
        basis=[
            f"{label}信号，信号时间 {bsp['dt']}",
            f"后续收盘站上 {confirmation:.3f} 确认",
            f"当前收盘跌破 {invalidation:.3f} 失效",
        ],
    )


def _build_sell_risk_decision(bars: list[dict], bsp: dict, freq: str) -> dict:
    signal_idx, signal_bar = _find_bsp_bar(bars, bsp, freq)
    if signal_bar is None:
        return _decision_with(ineligible_reason="signal_bar_missing")
    label = _BSP_LABELS.get(bsp["type"], bsp["type"])
    return _decision_with(
        bias="risk",
        state="invalid",
        bsp_type=bsp["type"],
        signal_dt=bsp["dt"],
        bars_since_signal=len(bars) - 1 - signal_idx,
        ineligible_reason="risk_structure",
        basis=[f"{label}信号，当前不具备做多条件"],
    )


def _find_active_breakout_bar(bars: list[dict], zg: float, zd: float,
                              zs_edt: str, freq: str) -> tuple[Optional[int], Optional[dict]]:
    """Find the first breakout since the latest completed-bar invalidation.

    The confirmed pivot's ``edt`` is the earliest scan boundary.  Scanning is
    strictly left-to-right over the completed bars already in this response;
    a close below ``zd`` clears an earlier breakout before a later close above
    ``zg`` can start a new lifecycle.
    """
    breakout_idx = None
    for index, bar in enumerate(bars):
        if _fmt_dt(bar["dt"], freq) < zs_edt:
            continue
        close = float(bar["close"])
        if close < zd:
            breakout_idx = None
        elif close > zg and breakout_idx is None:
            breakout_idx = index
    if breakout_idx is None:
        return None, None
    return breakout_idx, bars[breakout_idx]


def _build_breakout_decision(bars: list[dict], zs_break: str,
                             last_confirmed_zs: Optional[dict], freq: str) -> dict:
    if last_confirmed_zs is None:
        return _empty_decision()
    try:
        zg = round(float(last_confirmed_zs["zg"]), 3)
        zd = round(float(last_confirmed_zs["zd"]), 3)
    except (KeyError, TypeError, ValueError):
        return _empty_decision()

    if zs_break == "up":
        try:
            signal_idx, signal_bar = _find_active_breakout_bar(
                bars, zg, zd, last_confirmed_zs["edt"], freq,
            )
        except (KeyError, TypeError, ValueError):
            return _decision_with(ineligible_reason="signal_bar_missing")
        if signal_bar is None:
            return _decision_with(ineligible_reason="signal_bar_missing")
        trigger_high = round(zg * 1.01, 3)
        return _decision_with(
            bias="long",
            setup="zs_breakout",
            state="confirmed",
            signal_dt=_fmt_dt(signal_bar["dt"], freq),
            bars_since_signal=len(bars) - 1 - signal_idx,
            confirm_price=zg,
            invalidation_price=zd,
            trigger_price=zg,
            trigger_low=zg,
            trigger_high=trigger_high,
            candidate_eligible=True,
            ineligible_reason=None,
            basis=[
                f"收盘已向上突破中枢上沿 {zg:.3f}",
                f"回踩 {zg:.3f} 至 {trigger_high:.3f} 区间关注",
                f"跌破中枢下沿 {zd:.3f} 失效",
            ],
        )
    if zs_break == "down":
        return _decision_with(
            bias="risk",
            state="invalid",
            invalidation_price=zd,
            ineligible_reason="risk_structure",
            basis=[f"收盘已跌破中枢下沿 {zd:.3f}，当前不具备做多条件"],
        )
    return _empty_decision()


def _build_decision(bars: list[dict], bsp_list: list[dict], zs_break: str,
                    last_confirmed_zs: Optional[dict], freq: str) -> dict:
    """Build the deterministic, completed-bar-only Chan decision contract."""
    if not bars:
        return _empty_decision()

    buy_bsps = [bsp for bsp in bsp_list if bsp.get("type") in _BSP_BUY_TYPES]
    sell_bsps = [bsp for bsp in bsp_list if bsp.get("type") in _BSP_SELL_TYPES]
    buy_decision = _build_buy_decision(bars, buy_bsps[-1], freq) if buy_bsps else None

    if buy_decision and buy_decision["ineligible_reason"] == "signal_bar_missing":
        return buy_decision

    # A non-stale buy that has not been invalidated is the strongest structure.
    if buy_decision and buy_decision["candidate_eligible"]:
        return buy_decision

    breakout_decision = _build_breakout_decision(
        bars, zs_break, last_confirmed_zs, freq,
    )
    if breakout_decision["setup"] == "zs_breakout":
        return breakout_decision

    if sell_bsps:
        return _build_sell_risk_decision(bars, sell_bsps[-1], freq)
    if breakout_decision["bias"] == "risk":
        return breakout_decision

    # Expired or invalidated buys stay inspectable when no newer actionable
    # structure supersedes them.
    if buy_decision:
        return buy_decision
    return _empty_decision()


def _to_raw_bars(bars: list[dict], freq) -> list:
    return [
        RawBar(symbol="", id=i, dt=b["dt"], freq=freq,
               open=b["open"], close=b["close"], high=b["high"],
               low=b["low"], vol=b["vol"], amount=b.get("amount", 0))
        for i, b in enumerate(bars, 1)
    ]


def _eval_bsp(c) -> Optional[str]:
    """在当前 CZSC 状态（一根新笔刚完成）评估买卖点信号。命中返回中文名，否则 None。"""
    for fn in (cxt_first_buy_V221126, cxt_first_sell_V221126,
               cxt_second_bs_V240524, cxt_third_bs_V230319):
        try:
            sig = fn(c, di=1)
        except Exception:
            continue
        v1 = next(iter(sig.values()), "").split("_")[0]
        if v1 in _BSP_SIGNALS:
            return v1
    return None


def _detect_last_ex_div_gap(bars: list[dict], ts_code: str) -> Optional[int]:
    """最近一次机械跳空（除权除息）的 bar 下标；无则 None。

    判据：|今开/昨收 - 1| > 板限 + 1% 且昨日非涨跌停（涨跌停日的跳空是真实走势）。
    """
    limit = _board_limit_pct(ts_code) + 1.0
    last_idx = None
    # 判断“昨日是否涨跌停”必须有前前收，故从第 3 根开始。
    for i in range(2, len(bars)):
        prev_close = bars[i - 1]["close"]
        if prev_close <= 0:
            continue
        gap_pct = abs(bars[i]["open"] / prev_close - 1) * 100
        if gap_pct <= limit:
            continue
        prev_prev_close = bars[i - 2]["close"]
        prev_chg = abs(prev_close / prev_prev_close - 1) * 100 \
            if prev_prev_close > 0 else 0
        # 前一日接近板限（真实一字/涨跌停延续）则不算机械跳空
        if prev_chg >= _board_limit_pct(ts_code) - 1.0:
            continue
        last_idx = i
    return last_idx


def _serialize_bi(c, freq: str) -> list[dict]:
    finished = {(id(b.fx_a), id(b.fx_b)) for b in c.finished_bis}
    out = []
    for b in c.bi_list:
        out.append({
            "sdt": _fmt_dt(b.fx_a.dt, freq),
            "edt": _fmt_dt(b.fx_b.dt, freq),
            "direction": "up" if b.direction == Direction.Up else "down",
            "high": round(float(b.high), 3),
            "low": round(float(b.low), 3),
            "confirmed": (id(b.fx_a), id(b.fx_b)) in finished,
        })
    return out


def _serialize_zs(zs_seq, freq: str) -> list[dict]:
    """czsc 构造上最后一条恒延伸中，之前恒已完结（day-0 源码确认）。"""
    out = []
    for i, z in enumerate(zs_seq):
        out.append({
            "sdt": _fmt_dt(z.sdt, freq),
            "edt": _fmt_dt(z.edt, freq),
            "zg": round(float(z.zg), 3),
            "zd": round(float(z.zd), 3),
            "zz": round(float(z.zz), 3),
            "state": "extending" if i == len(zs_seq) - 1 else "confirmed",
        })
    return out


def _zs_break(close: float, zs_list: list[dict], gap_idx: Optional[int],
              bars: list[dict], freq: str) -> str:
    """最新收盘价 vs 最后一个已确认中枢（且完全形成于最近一次机械跳空之后）。

    >zg→up, <zd→down, 其间→inside, 无→none。
    """
    gap_dt = bars[gap_idx]["dt"] if gap_idx is not None else None
    confirmed = [z for z in zs_list if z["state"] == "confirmed"]
    if gap_dt is not None:
        confirmed = [z for z in confirmed
                     if z["sdt"] > _fmt_dt(gap_dt, freq)]
    if not confirmed:
        return "none", None
    z = confirmed[-1]
    if close > z["zg"]:
        return "up", z
    if close < z["zd"]:
        return "down", z
    return "inside", z


def get_structure(ts_code: str, freq: str = "D", n: int = 250) -> dict:
    """缠论结构主入口。返回可 JSON 序列化 dict。

    降级路径：bars < 30 → 200 + summary.reason=insufficient_bars（K 线照画）。
    数据源全挂 → fetch_raw_bars 抛 DataFetchError（backend 502）。
    czsc 未装 → ChanUnavailableError（backend 501）。
    """
    if not _CZSC_AVAILABLE:
        raise ChanUnavailableError("czsc 未安装，缠论结构不可用")
    if freq not in _FREQ_MAP:
        raise ValueError(f"不支持的周期: {freq}")

    ts_code = data.normalize_ts_code(ts_code)
    name = data.get_name_map().get(ts_code, "")
    bars = fetch_raw_bars(ts_code, n=n, freq=freq)

    resp = {
        "ts_code": ts_code,
        "name": name,
        "freq": freq,
        "bars": [{
            "dt": _fmt_dt(b["dt"], freq),
            "open": b["open"], "high": b["high"], "low": b["low"],
            "close": b["close"], "vol": b["vol"],
        } for b in bars],
        "bi_list": [],
        "zs_list": [],
        "bsp_list": [],
        "decision": _empty_decision(),
        "summary": {},
    }
    bars_end_dt = _fmt_dt(bars[-1]["dt"], freq) if bars else None
    if len(bars) < 30:
        resp["summary"] = {"reason": "insufficient_bars", "bars_end_dt": bars_end_dt}
        return resp

    raw_bars = _to_raw_bars(bars, getattr(Freq, _FREQ_MAP[freq]))

    # 增量构建 + 每笔完成时评估 BSP（day-0：增量与批量结果一致）
    c = CZSC(raw_bars[:30])
    bsp_list = []
    seen_bi = len(c.finished_bis)
    for rb in raw_bars[30:]:
        c.update(rb)
        if len(c.finished_bis) > seen_bi:
            seen_bi = len(c.finished_bis)
            hit = _eval_bsp(c)
            if hit:
                bi = c.finished_bis[-1]
                bsp_list.append({
                    "dt": _fmt_dt(bi.fx_b.dt, freq),
                    "type": _BSP_TYPE_MAP[hit],
                    "price": round(float(bi.fx_b.fx), 3),
                    "approximate": True,  # 本级别近似，未经次级别确认
                })

    zs_seq = get_zs_seq(c.bi_list)
    bi_list = _serialize_bi(c, freq)
    zs_list = _serialize_zs(zs_seq, freq)

    gap_idx = _detect_last_ex_div_gap(bars, ts_code)
    last_close = bars[-1]["close"]
    zb, zb_zs = _zs_break(last_close, zs_list, gap_idx, bars, freq)
    decision = _build_decision(bars, bsp_list, zb, zb_zs, freq)

    last_bi = bi_list[-1] if bi_list else None
    last_bi_days = 0
    if last_bi:
        last_bi_days = sum(1 for b in bars
                           if _fmt_dt(b["dt"], freq) >= last_bi["sdt"])

    resp["bi_list"] = bi_list
    resp["zs_list"] = zs_list
    resp["bsp_list"] = bsp_list
    resp["decision"] = decision
    resp["summary"] = {
        "bars_end_dt": bars_end_dt,
        "ex_div_gap": gap_idx is not None,
        "last_bi_direction": last_bi["direction"] if last_bi else None,
        "last_bi_days": last_bi_days,
        "last_bi_confirmed": last_bi["confirmed"] if last_bi else None,
        "last_confirmed_zs": zb_zs,  # 与 zs_break 同口径（跳空后过滤）
        "extending_zs": zs_list[-1] if zs_list and zs_list[-1]["state"] == "extending" else None,
        "zs_break": zb,
        "recent_bsp": bsp_list[-1] if bsp_list else None,
    }
    return resp
