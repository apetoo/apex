"""Playstyle Engine - Feature Extractor (FE)。

标的玩法判定（打野/波段/中线/长线）的 Python 特征层。算 10 个玩法特征 + 完整度 +
risk_level，供 analyze prompt 注入（**不是 AI 工具**，与 market_sentiment / 分时一致的
注入式设计--设计上不让 AI 决定调不调）。复用 growth-stock 范式链的 Python 特征段。

设计文档：~/.gstack/projects/apex/wanmingyu-v9-design-20260714-105554.md
Review Decisions 落地：
- A2  FE 10 特征数持久化为 playstyle_features（本模块产出 dict，T5 落 entry 字段）。
- A3  依赖 signals/{moneyflow,northbound,limit_up} + data.get_moneyflow 的按日缓存（T3）。
- D11 换手率从缓存日线自推（vol*close/circ_mv），不增 market-wide 调用。
- D12 业绩用多季 or_yoy 趋势（最近 2-4 季），不用单季 quarters[0]。
- D13 risk_level = low/medium/high，Python 从 FE 算，与 playstyle 正交（非第 6 档玩法）。
- 失败路径  每特征 fail-soft -> None + present=False；completeness<0.5 -> playstyle 允许
  null、跳过硬校验、契合度 short-circuit insufficient_data（绝不卡死 analyze loop）。

v1 prototype-first（D9）：本模块只算特征；星级 prototype（T4）与 verdict 适配（T5）消费之。
"""
import json
import math
import statistics
import threading
from datetime import datetime
from typing import Optional

from apex import data

# ── 10 FE 特征（completeness 分母，加减特征改这里 + extract_features）──────────────
FEATURE_NAMES = [
    "volatility",    # 波动率 20d/60d 年化对数收益标准差
    "turnover",      # 换手率 20日均值（D11 自推）
    "moneyflow",     # 主力净流入 近5日 趋势
    "northbound",    # 北向变动（v1 当日代理，5日历史 fetcher 不支持 -> A3 错配）
    "limit_up",      # 连板高度/涨停 近10日
    "or_yoy",        # 业绩增速（D12 多季）
    "circ_mv",       # 流通市值
    "valuation",     # pe_ttm / pb
    "roe",           # ROE
    "ma_alignment",  # MA 排列完整度 {MA5,MA10,MA20,MA60}
]

# 4 档玩法（schema 是 list，可折叠；Open Q #1 已定为 4 档）
PLAYSTYLES = ["打野", "波段", "中线", "长线"]

# FE 进程级缓存：(ts_code, as_of) -> features dict。稳定性 re-run（D8）同股同日近零成本。
# 与 T3 的按日信号缓存互补：信号缓存跨股共享同日市场数据，FE 缓存跨 run 共享同股结果。
_FE_LOCK = threading.Lock()
_FE_CACHE: dict[tuple[str, str], dict] = {}
_FE_SENTINEL = object()


def clear_fe_cache() -> int:
    """清 FE 缓存，返回清除条数。"""
    with _FE_LOCK:
        n = len(_FE_CACHE)
        _FE_CACHE.clear()
        return n


# ── 小工具 ──────────────────────────────────────────────────────────────────────

def _safe_float(v, default=None):
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _slope(vals: list[float]) -> float:
    """最小二乘斜率（按日 index）。n<2 返回 0。"""
    n = len(vals)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(vals) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, vals))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def _median(vals: list[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return statistics.median(vals)


def _today_yyyymmdd() -> str:
    return datetime.today().strftime("%Y%m%d")


# ── 单特征提取（每个返回 (feature_dict, present_bool)，fail-soft）──────────────────

def _feat_volatility(bars: list[dict]) -> tuple[dict, bool]:
    """20日 + 60日 年化对数收益标准差（%）。bars: get_daily_price 解析后的 list。"""
    closes = [_safe_float(b.get("close")) for b in bars]
    closes = [c for c in closes if c is not None and c > 0]
    if len(closes) < 2:
        return {}, False
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if not rets:
        return {}, False

    def _ann_vol(window):
        seg = rets[-window:]
        if len(seg) < 2:
            return None
        m = sum(seg) / len(seg)
        var = sum((r - m) ** 2 for r in seg) / (len(seg) - 1)
        return round(math.sqrt(var) * math.sqrt(252) * 100, 2)

    return {
        "vol_20d_pct": _ann_vol(20),
        "vol_60d_pct": _ann_vol(60),
    }, True


def _feat_turnover(bars: list[dict], circ_mv_yuan: Optional[float]) -> tuple[dict, bool]:
    """换手率 20日均值（%）-- D11 自推：turnover_rate% = vol*close*10000/circ_mv_yuan。

    vol(tushare daily, 手) × 100 = 成交股数；流通股本 = circ_mv_yuan/close。
    turnover% = 成交股数/流通股本×100 = vol*close*10000/circ_mv_yuan（vol 手/close 元/circ_mv 元）。
    **circ_mv_yuan 必须是元**：get_fundamentals 的 circ_mv 原始是万元，extract_features 已 ×1e4 转元。
    用 latest circ_mv 近似历史各日（流通股本短期稳定）；adj_factor≠1 时 qfq vol/close 有微小误差，
    校准阶段评估（Open Q #3）。circ_mv 缺 -> 退化为 fundamentals 最新 turnover_rate。
    """
    latest_rate = None  # 由调用方 fundamentals 注入更准的 latest，这里只算 20日均值
    if not circ_mv_yuan or circ_mv_yuan <= 0:
        return {"avg_20d_pct": latest_rate, "latest_pct": None}, False
    vals = []
    for b in bars[-20:]:
        vol = _safe_float(b.get("vol"))
        close = _safe_float(b.get("close"))
        if vol is None or close is None or close <= 0:
            continue
        vals.append(vol * close * 10000 / circ_mv_yuan)
    if not vals:
        return {"avg_20d_pct": None, "latest_pct": None}, False
    return {
        "avg_20d_pct": round(sum(vals) / len(vals), 3),
        "latest_pct": round(vals[-1], 3),
    }, True


def _feat_moneyflow(ts_code: str, as_of: str) -> tuple[dict, bool]:
    """主力净流入 近5日：sum 符号 + 斜率 + 连续正天数。net_mf_amount 已是元（data.get_moneyflow ×1e4）。"""
    dates = data.last_n_trade_dates(as_of, 5)
    if not dates:
        return {}, False
    series: list[Optional[float]] = []
    for d in dates:
        amt = None
        try:
            rows = json.loads(data.get_moneyflow(d) or "[]")
            for r in rows:
                if str(r.get("ts_code", "")) == ts_code:
                    amt = _safe_float(r.get("net_mf_amount"), 0.0) or 0.0
                    break
        except Exception:
            amt = None
        series.append(amt)
    # 全缺 -> 不 present；部分缺用 0 占位算趋势（标记 partial）
    present_vals = [v for v in series if v is not None]
    if not present_vals:
        return {}, False
    filled = [v if v is not None else 0.0 for v in series]
    total = sum(filled)
    # 连续正天数（从最近一日往回）
    consec = 0
    for v in reversed(filled):
        if v > 0:
            consec += 1
        else:
            break
    return {
        "sum_yuan": round(total, 0),
        "sign": 1 if total > 0 else (-1 if total < 0 else 0),
        "slope": round(_slope(filled), 0),
        "consec_positive": consec,
        "partial": any(v is None for v in series),
    }, True


def _feat_northbound(ts_code: str, as_of: str) -> tuple[dict, bool]:
    """北向变动 -- v1 当日代理：northbound.fetch 忽略 trade_date 只返今日榜。
    近5日持股变化方向需 per-stock 历史接口，当前 fetcher 不支持（A3 错配），v1 降级为
    今日是否上榜 + 净流入额。5日历史推迟。
    """
    try:
        from apex.signals import northbound
        recs = northbound.fetch(as_of)
    except Exception:
        return {}, False
    if recs is None:
        return {}, False
    inflow = None
    for r in recs:
        if str(r.get("ts_code", "")) == ts_code:
            inflow = _safe_float((r.get("raw") or {}).get("inflow_mv"))
            break
    return {
        "listed_today": inflow is not None,
        "today_inflow_yuan": round(inflow, 0) if inflow is not None else None,
    }, True  # fetch 成功即 present（未上榜也是有效信息：今日无北向大额买入）


def _feat_limit_up(ts_code: str, as_of: str) -> tuple[dict, bool]:
    """近10日 最高连板高度 + 涨停次数。limit_up.fetch(d) 返回当日涨停池（含 limit_times）。"""
    dates = data.last_n_trade_dates(as_of, 10)
    if not dates:
        return {}, False
    try:
        from apex.signals import limit_up
    except Exception:
        return {}, False
    hit_days = 0
    max_consec = 0
    latest_consec = 0
    fetched_any = False
    for i, d in enumerate(dates):
        try:
            recs = limit_up.fetch(d)
        except Exception:
            recs = None
        if recs is None:
            continue
        fetched_any = True
        for r in recs:
            if str(r.get("ts_code", "")) == ts_code:
                lt = int((r.get("raw") or {}).get("limit_times", 1) or 1)
                hit_days += 1
                max_consec = max(max_consec, lt)
                if i == len(dates) - 1:
                    latest_consec = lt
                break
    if not fetched_any:
        return {}, False
    return {
        "count_10d": hit_days,
        "max_consecutive_10d": max_consec,
        "latest_consecutive": latest_consec,
    }, True


def _feat_or_yoy(fund: dict) -> tuple[dict, bool]:
    """业绩增速 -- D12 多季 or_yoy：latest + median + 趋势 + 是否持续高（≥15%）。
    不用单季 quarters[0]，避免单季快照噪声系统性误杀复合成长长线股。
    """
    quarters = fund.get("quarters") or []
    vals = [_safe_float(q.get("or_yoy")) for q in quarters]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {}, False
    # 趋势：近季 vs 远季（quarters 降序，vals[0] 最新）
    trend = None
    if len(vals) >= 2:
        recent = vals[0]
        older = vals[-1]
        if abs(recent - older) < 5:
            trend = "stable"
        elif recent > older:
            trend = "improving"
        else:
            trend = "declining"
    sustained_high = len(vals) >= 2 and all(v >= 15 for v in vals[:min(len(vals), 4)])
    return {
        "latest": round(vals[0], 2),
        "median": round(_median(vals) or 0, 2),
        "trend": trend,
        "sustained_high": sustained_high,
        "quarters_n": len(vals),
    }, True


def _feat_circ_mv(circ_mv_yuan: Optional[float]) -> tuple[dict, bool]:
    """流通市值（元 + 亿）。circ_mv_yuan 已由 extract_features 从 tushare 万元转成元。"""
    if not circ_mv_yuan or circ_mv_yuan <= 0:
        return {}, False
    return {"yuan": round(circ_mv_yuan, 0), "yi": round(circ_mv_yuan / 1e8, 2)}, True


def _feat_valuation(fund: dict) -> tuple[dict, bool]:
    """估值 pe_ttm / pb。"""
    v = fund.get("valuation") or {}
    pe = _safe_float(v.get("pe_ttm"))
    pb = _safe_float(v.get("pb"))
    if pe is None and pb is None:
        return {}, False
    return {"pe_ttm": pe, "pb": pb}, True


def _feat_roe(fund: dict) -> tuple[dict, bool]:
    """ROE：latest（累计口径，fina_indicator）+ 近4季是否全正。"""
    quarters = fund.get("quarters") or []
    vals = [_safe_float(q.get("roe")) for q in quarters]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {}, False
    all_positive = len(vals) >= 4 and all(v > 0 for v in vals[:4])
    return {
        "latest": round(vals[0], 2),
        "all_positive_4q": all_positive,
        "quarters_n": len(vals),
    }, True


def _feat_ma_alignment(bars: list[dict]) -> tuple[dict, bool]:
    """MA 排列完整度：{MA5,MA10,MA20,MA60} 相邻短>长 计数（0-3）+ 完整多头排列 flag。"""
    if not bars:
        return {}, False
    b = bars[-1]
    ma5 = _safe_float(b.get("ma5"))
    ma10 = _safe_float(b.get("ma10"))
    ma20 = _safe_float(b.get("ma20"))
    ma60 = _safe_float(b.get("ma60"))
    pairs = [(ma5, ma10), (ma10, ma20), (ma20, ma60)]
    score = sum(1 for a, bb in pairs if a is not None and bb is not None and a > bb)
    if all(a is None and bb is None for a, bb in pairs):
        return {}, False
    return {
        "score": score,                  # 0-3，相邻短>长条数
        "perfect_bullish": score == 3,   # MA5>MA10>MA20>MA60
        "ma60_present": ma60 is not None,
    }, True


# ── risk_level（D13，与 playstyle 正交）──────────────────────────────────────────

def compute_risk_level(features: dict) -> str:
    """low/medium/high，Python 从 FE 特征算。v1 初值（Open Q #3 待校准）：
    high   = 60日年化波动率>60% 或 流通市值<50亿(小盘) 或 业绩趋势 declining/最新<0
    low    = 60日波动率<30% 且 流通市值>200亿 且 or_yoy 最新>10%
    medium = 其余
    缺关键特征 -> 不轻易判 low，倾向 medium（诚实降级）。
    """
    vol = (features.get("volatility") or {}).get("vol_60d_pct")
    mv = (features.get("circ_mv") or {}).get("yi")
    or_feat = features.get("or_yoy") or {}
    or_latest = or_feat.get("latest")
    or_trend = or_feat.get("trend")

    high = (
        (vol is not None and vol > 60)
        or (mv is not None and mv < 50)
        or (or_trend == "declining")
        or (or_latest is not None and or_latest < 0)
    )
    if high:
        return "high"
    low = (
        vol is not None and vol < 30
        and mv is not None and mv > 200
        and or_latest is not None and or_latest > 10
    )
    if low:
        return "low"
    return "medium"


def compute_completeness(features: dict) -> float:
    """present 特征数 / 10。"""
    present = sum(1 for k in FEATURE_NAMES if (features.get(k) or {}).get("present"))
    return round(present / len(FEATURE_NAMES), 2)


# ── 主入口 ──────────────────────────────────────────────────────────────────────

def extract_features(ts_code: str, as_of: Optional[str] = None) -> dict:
    """算 10 个玩法特征 + completeness + risk_level。fail-soft，绝不抛异常卡死调用方。

    as_of: YYYYMMDD（默认今天）。last_n_trade_dates 处理非交易日，返回 <=as_of 的交易日。
    返回 dict（T5 落 entry.playstyle_features；T4 prototype 消费 features 算星级）：
      {ts_code, as_of, features:{<name>:{...,present:bool}}, completeness, risk_level, notes}
    """
    ts_code = data.normalize_ts_code(ts_code)
    as_of = (as_of or _today_yyyymmdd()).replace("-", "")
    cache_key = (ts_code, as_of)
    cached = _FE_CACHE.get(cache_key, _FE_SENTINEL)
    if cached is not _FE_SENTINEL:
        return cached

    notes: list[str] = []
    features: dict = {}

    # ── 一次 get_daily_price（波动率/换手/MA 共享）──
    bars: list[dict] = []
    try:
        bars = json.loads(data.get_daily_price(ts_code) or "[]")
    except Exception:
        bars = []

    # ── 一次 get_fundamentals（换手 circ_mv/业绩/市值/估值/ROE 共享）──
    fund: dict = {}
    try:
        fund = json.loads(data.get_fundamentals(ts_code) or "{}")
    except Exception:
        fund = {}
    # tushare daily_basic circ_mv 原始单位是万元（get_fundamentals 未转，与 get_market_daily_basic
    # 的 ×1e4 不同），统一转成元供 _feat_circ_mv / _feat_turnover(D11) 使用。
    circ_mv_wan = _safe_float((fund.get("valuation") or {}).get("circ_mv"))
    circ_mv_yuan = circ_mv_wan * 1e4 if circ_mv_wan else None

    # 逐特征 fail-soft
    for name, feat, present in [
        ("volatility", *_feat_volatility(bars)),
        ("turnover", *_feat_turnover(bars, circ_mv_yuan)),
        ("moneyflow", *_feat_moneyflow(ts_code, as_of)),
        ("northbound", *_feat_northbound(ts_code, as_of)),
        ("limit_up", *_feat_limit_up(ts_code, as_of)),
        ("or_yoy", *_feat_or_yoy(fund)),
        ("circ_mv", *_feat_circ_mv(circ_mv_yuan)),
        ("valuation", *_feat_valuation(fund)),
        ("roe", *_feat_roe(fund)),
        ("ma_alignment", *_feat_ma_alignment(bars)),
    ]:
        feat["present"] = present
        features[name] = feat

    # turnover latest_pct 优先用 fundamentals 的 turnover_rate（daily_basic 原值，比自推准）
    if features["turnover"].get("present"):
        tr = _safe_float((fund.get("valuation") or {}).get("turnover_rate"))
        if tr is not None:
            features["turnover"]["latest_pct"] = round(tr, 3)
            features["turnover"]["latest_source"] = "daily_basic"

    # 降级说明（给 T5/AI 透明：哪些特征降级了，不编结论）
    if not features["turnover"].get("present") and circ_mv_yuan:
        notes.append("turnover 自推缺有效 bar，已 fail-soft")
    if features["moneyflow"].get("partial"):
        notes.append("moneyflow 5日有部分日缺失，用 0 占位算趋势")
    if not features["northbound"].get("present"):
        notes.append("northbound fetch 失败，当日代理缺失（5日历史本就 v1 未支持）")

    completeness = compute_completeness(features)
    risk_level = compute_risk_level(features)

    result = {
        "ts_code": ts_code,
        "as_of": as_of,
        "features": features,
        "completeness": completeness,
        "risk_level": risk_level,
        "notes": notes,
    }
    with _FE_LOCK:
        _FE_CACHE[cache_key] = result
    return result


# ── T4: Python 规则星级 prototype（D9 prototype-first）──────────────────────────
#
# 用 FE 特征按设计 rubric 表（5★/3★/1★ 锚点）算 0-5 星级。这是 D9 的"最便宜验证"--
# 在 T1 基准集跑 hit-rate，≥70% 则不上 playstyle.md skill、不上 D8 rig（AI 只在 evidence
# 写玩法原因）；<70% 才上 skill rubric + D8 rig。
#
# ⚠ 已知偏差（设计使然，非 bug）：打野 5★(题材龙头) / 中线 5★(行业景气) 的锚点依赖
# web_search/AI 判断，**不在 FE 特征内**，故 Python prototype 把这两档 cap 在 4★。这系统性
# 压低 5★ 标定精度，但 v1 success criteria 只验 `top`(argmax) 命中率（Open Q #6：4★ vs 5★
# 标定无验收，推迟 v2），故 prototype 仍可决 D9 分支。波段/长线 5★ 全 FE 可达，不 cap。
# 阈值均为 v1 初值（Open Q #3，跑验收集后校准）。

def _rate_daye(f: dict) -> tuple[int, list[str]]:
    """打野：连板 + 换手 + 涨停次数。cap 4（题材龙头需 AI）。"""
    lu = f.get("limit_up") or {}
    tr = f.get("turnover") or {}
    max_consec = lu.get("max_consecutive_10d") or 0
    count = lu.get("count_10d") or 0
    turnover = tr.get("avg_20d_pct")
    reasons: list[str] = []

    if max_consec >= 3:
        limit_pts = 2.0
        reasons.append(f"游资属性(近10日最高{max_consec}连板)")
    elif max_consec == 2:
        limit_pts = 1.5
    elif max_consec == 1:
        limit_pts = 1.0
    else:
        limit_pts = 0.0

    if turnover is not None:
        if turnover > 15:
            tr_pts = 1.5
            reasons.append(f"情绪驱动(换手{turnover}%>15%)")
        elif turnover > 10:
            tr_pts = 1.0
        elif turnover > 5:
            tr_pts = 0.5
        else:
            tr_pts = 0.0
    else:
        tr_pts = 0.0

    cnt_pts = 1.0 if count >= 3 else (0.5 if count >= 1 else 0.0)

    rating = min(4, round(limit_pts + tr_pts + cnt_pts))
    return rating, reasons


def _rate_band(f: dict) -> tuple[int, list[str]]:
    """波段：MA 多头排列 + 主力净流入 + 波动适中。5★ 全 FE 可达，不 cap。"""
    ma = f.get("ma_alignment") or {}
    mf = f.get("moneyflow") or {}
    vol = f.get("volatility") or {}
    score = ma.get("score") or 0
    consec = mf.get("consec_positive") or 0
    mf_sign = mf.get("sign") or 0
    vol20 = vol.get("vol_20d_pct")
    reasons: list[str] = []

    if score == 3:
        ma_pts = 2.0
        reasons.append("趋势完整度(MA5>10>20>60 完整多头排列)")
    elif score == 2:
        ma_pts = 1.5
    elif score == 1:
        ma_pts = 1.0
    else:
        ma_pts = 0.0

    if consec >= 5 and mf_sign > 0:
        mf_pts = 2.0
        reasons.append(f"资金一致性(主力连续{consec}日净流入)")
    elif consec >= 3 and mf_sign > 0:
        mf_pts = 1.5
    elif consec >= 1 and mf_sign > 0:
        mf_pts = 1.0
    else:
        mf_pts = 0.0

    if vol20 is not None:
        if 20 <= vol20 <= 50:
            v_pts = 1.0
        elif vol20 < 20 or 50 < vol20 <= 80:
            v_pts = 0.5
        else:
            v_pts = 0.0
    else:
        v_pts = 0.0

    rating = min(5, round(ma_pts + mf_pts + v_pts))
    return rating, reasons


def _rate_mid(f: dict) -> tuple[int, list[str]]:
    """中线：or_yoy + 估值合理 + ROE。cap 4（行业景气需 AI）。业绩下滑 -> 压至 1。"""
    ory = f.get("or_yoy") or {}
    val = f.get("valuation") or {}
    roe = f.get("roe") or {}
    latest = ory.get("latest")
    trend = ory.get("trend")
    pe = val.get("pe_ttm")
    roe_l = roe.get("latest")
    reasons: list[str] = []

    if latest is not None:
        if latest > 20:
            o_pts = 2.0
            reasons.append(f"业绩支撑(or_yoy {latest}%>20%)")
        elif latest > 10:
            o_pts = 1.5
        elif latest > 5:
            o_pts = 1.0
        else:
            o_pts = 0.0
    else:
        o_pts = 0.0

    if pe is not None and pe > 0:
        if 15 <= pe <= 40:
            v_pts = 1.0
        elif pe < 15 or 40 < pe <= 80:
            v_pts = 0.5
        else:
            v_pts = 0.0
    else:
        v_pts = 0.0

    if roe_l is not None:
        if roe_l > 10:
            r_pts = 1.0
        elif roe_l > 5:
            r_pts = 0.5
        else:
            r_pts = 0.0
    else:
        r_pts = 0.0

    rating = min(4, round(o_pts + v_pts + r_pts))
    if trend == "declining":
        rating = min(rating, 1)
    return rating, reasons


def _rate_long(f: dict) -> tuple[int, list[str]]:
    """长线：or_yoy 持续高 + ROE 稳定高 + 估值低位。5★ 全 FE 可达。业绩下滑 -> 压至 1。"""
    ory = f.get("or_yoy") or {}
    val = f.get("valuation") or {}
    roe = f.get("roe") or {}
    sus = ory.get("sustained_high")
    latest = ory.get("latest")
    trend = ory.get("trend")
    pe = val.get("pe_ttm")
    pb = val.get("pb")
    roe_l = roe.get("latest")
    all_pos = roe.get("all_positive_4q")
    reasons: list[str] = []

    if sus:
        o_pts = 2.0
        reasons.append(f"业绩支撑(or_yoy {latest}% 多季持续高≥15%)")
    elif latest is not None and latest > 15:
        o_pts = 1.5
    elif latest is not None and latest > 10:
        o_pts = 1.0
    else:
        o_pts = 0.0

    if roe_l is not None:
        if all_pos and roe_l > 12:
            r_pts = 2.0
            reasons.append(f"ROE稳定({roe_l}% 近4季全正)")
        elif roe_l > 8:
            r_pts = 1.0
        else:
            r_pts = 0.0
    else:
        r_pts = 0.0

    low_val = (pe is not None and 0 < pe < 20) or (pb is not None and 0 < pb < 1.5)
    mid_val = (pe is not None and 20 <= pe < 30) or (pb is not None and 1.5 <= pb < 2.5)
    if low_val:
        v_pts = 1.5
        reasons.append(f"估值水位(PE {pe}/PB {pb} 低位)")
    elif mid_val:
        v_pts = 1.0
    else:
        v_pts = 0.0

    rating = min(5, round(o_pts + r_pts + v_pts))
    if trend == "declining":
        rating = min(rating, 1)
    return rating, reasons


_RATERS = [  # (name, fn) -- 顺序即 PLAYSTYLES，平局 tie-break 取首个
    ("打野", _rate_daye),
    ("波段", _rate_band),
    ("中线", _rate_mid),
    ("长线", _rate_long),
]


def compute_rule_ratings(feats: dict) -> dict:
    """Python 规则星级 prototype（T4 / D9）。

    输入 extract_features(...) 的结果。completeness<0.5 -> usable=False（playstyle=null 路径）。
    返回与 AI `playstyle` 字段同形（T5 ≥70% 路径直接 post-hoc 套用）：
      {ratings:{打野,波段,中线,长线 0-5}, top, reasons[], method, usable, [note]}
    reasons 引用 FE 特征数字（success criteria「可解释」）。平局取首个并说明（C2 严格
    tied-for-max 仅 skill 分支启用，prototype 简单 tie-break）。
    """
    if not feats or feats.get("completeness", 0) < 0.5:
        return {
            "ratings": None, "top": None, "reasons": [],
            "method": "python_rule_v1", "usable": False,
            "note": "特征不足(completeness<0.5)，playstyle=null",
        }
    f = feats["features"]
    scored = [(name, rater(f)) for name, rater in _RATERS]
    ratings = {name: sc[0] for name, sc in scored}
    reasons_by_name = {name: sc[1] for name, sc in scored}
    mx = max(ratings.values()) if ratings else 0
    top_candidates = [name for name, _ in _RATERS if ratings[name] == mx]
    top = top_candidates[0] if top_candidates else None

    reasons: list[str] = list(reasons_by_name.get(top, [])) if top else []
    if len(top_candidates) > 1 and mx > 0:
        reasons.append(f"平局({'+'.join(top_candidates)}={mx}★)，prototype 取首个")
    if mx == 0:
        reasons = ["各玩法均无强特征命中，top 取默认首个(0★)"]
    elif not reasons:
        reasons.append(f"{top} {mx}★（规则命中）")

    return {
        "ratings": ratings,
        "top": top,
        "reasons": reasons,
        "method": "python_rule_v1",
        "usable": True,
        "low_confidence": mx <= 1,
    }


def evaluate_benchmark(benchmark: list[dict], as_of: Optional[str] = None) -> dict:
    """在 T1 基准集跑 prototype hit-rate，决定 D9 分支（T4 验收）。

    benchmark: [{ts_code, expected_top, why}]，expected_top ∈ PLAYSTYLES。
    网络调用（tushare/akshare），首次慢；FE 缓存 + 信号按日缓存使重复跑近零成本。
    返回 {n, usable, hit_rate, tie_hit_rate, d9_branch, results[]}。
    hit_rate>=0.7 -> d9_branch=python_rule（不上 skill）；否则 skill_rubric。
    """
    results = []
    hits = tie_hits = usable_n = 0
    for item in benchmark:
        ts = data.normalize_ts_code(item["ts_code"])
        expected = item.get("expected_top")
        feats = extract_features(ts, as_of=as_of)
        rule = compute_rule_ratings(feats)
        ratings = rule.get("ratings") or {}
        mx = max(ratings.values()) if ratings else 0
        top_candidates = [k for k in PLAYSTYLES if ratings.get(k) == mx] if ratings else []
        is_hit = rule.get("top") == expected
        is_tie_hit = expected in top_candidates
        if rule.get("usable"):
            usable_n += 1
        if is_hit:
            hits += 1
        if is_tie_hit:
            tie_hits += 1
        results.append({
            "ts_code": ts,
            "expected": expected,
            "top": rule.get("top"),
            "ratings": ratings,
            "hit": is_hit,
            "tie_hit": is_tie_hit,
            "completeness": feats["completeness"],
            "usable": rule.get("usable"),
            "low_confidence": rule.get("low_confidence"),
            "reasons": rule.get("reasons"),
            "why_expected": item.get("why", ""),
        })
    n = len(benchmark)
    hit_rate = round(hits / n, 3) if n else 0.0
    return {
        "n": n,
        "usable": usable_n,
        "hit_rate": hit_rate,
        "tie_hit_rate": round(tie_hits / n, 3) if n else 0.0,
        "d9_branch": "python_rule (skip skill/D8 rig)" if hit_rate >= 0.7 else "skill_rubric (build skill + D8 rig)",
        "results": results,
    }


# ── T6: 玩家契合度（v1 脚手架，D10/OV#4）─────────────────────────────────────────
#
# 契合度是 Playstyle Engine 的第一个下游消费者。v1 当前画像 n<20（ADR-0002 样本门控），
# 按门控对所有票返回 insufficient_data，**matched/mismatch 四档规则推迟 v1.1**（随 A1
# stop_honored_rate 路径修复 / C1 per-field confidence 门控 / OV#5 hold_period_distribution
# 桶 一起建并真实验证）。v1 只留 `playstyle_fit` 字段脚手架，entry 形状稳定，UI 显示徽章。
#
# v1.1 实现将：load system 画像 -> 按 top 玩法比对 avg_hold_days/hold_period_distribution/
# stop_honored_rate -> matched/mismatch + 错配维度简述（见设计文档下游表）。

def compute_playstyle_fit(playstyle) -> dict:
    """v1 脚手架：始终 insufficient_data。

    playstyle: entry 的 playstyle 值（None=FE completeness<0.5，或 {ratings,primary,...}）。
    返回 {state, note}：
      - playstyle is None -> insufficient_data, note='特征不足'（short-circuit，不查画像）
      - 否则 -> insufficient_data, note='画像累积中'（n<20，规则 v1.1 启用）
    """
    if playstyle is None:
        return {"state": "insufficient_data", "note": "特征不足（FE 完整度<0.5）"}
    return {"state": "insufficient_data", "note": "画像累积中（n<20，契合度规则 v1.1 启用）"}


# ── verdict 适配：finalize_playstyle（D9 skill 分支，AI 填 + 软门控）───────────────
#
# skill 分支（D9 <70% 后启用）：AI 经 record_verdict 填 playstyle；本函数规整成 entry 形状
# {ratings, primary, secondary, reasons, method, low_confidence} 并跑两条软门控。
#
# **门控为软（不 reject）--OV#6 解析**：record_verdict 已有 3 条 reject（强制类别/价位/静态PE），
# 再叠 playstyle 2 条硬 reject 易撞 max_tool_iterations=12 兜底，D8 跑批时部分票会无 verdict。
# 故 playstyle 门控走 auto-fix/flag：gate#1 自动对齐 primary 到 tied-for-max；gate#2 标 low_confidence。
# 不阻断 verdict、不循环。D8 后若 skill 仍频繁误判再考虑硬化。
#
# **OV#2 解析**：gate#2 的"业绩弱"用 sustained-weak（多季 or_yoy 未持续≥15%），非单季 None--
# completeness≥0.5 时 or_yoy 仍可能单季缺失（fina_indicator 限频/停牌），单季 None 当 AI 错误
# reject 违反"失败路径不卡死 loop"。sustained-weak 判多季连续弱才标记。

def _clamp(v, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return lo


def _second_highest(ratings: dict) -> Optional[str]:
    """次高星玩法（同分取 PLAYSTYLES 顺序靠前）。次高 ≤0 或仅一档有分 -> None。"""
    if not ratings:
        return None
    items = sorted(((ratings.get(n, 0), n) for n in PLAYSTYLES),
                   key=lambda x: (-x[0], PLAYSTYLES.index(x[1])))
    if len(items) >= 2 and items[1][0] > 0 and items[1][0] < items[0][0]:
        return items[1][1]
    # 同分并列（items[1][0]==items[0][0]）-> 取并列中的下一个作为 secondary
    if len(items) >= 2 and items[1][0] > 0 and items[1][0] == items[0][0]:
        return items[1][1]
    return None


def finalize_playstyle(ai_playstyle, feats: dict) -> Optional[dict]:
    """规整 AI 填的 playstyle（或 Python fallback）为 entry 形状 + 软门控。

    - FE completeness<0.5 -> None（playstyle=null 路径）。
    - AI 填了 ratings -> 用 AI 的，method=ai_skill，gate#1 自动对齐 primary 到 tied-for-max。
    - AI 未填但 FE>=0.5 -> Python compute_rule_ratings fallback，method=python_fallback。
    - gate#2：primary=长线 + 60日波动率>80% + sustained-weak -> low_confidence 标记（软）。
    """
    completeness = (feats or {}).get("completeness", 0)
    if completeness < 0.5:
        return None

    f = (feats or {}).get("features", {})
    vol60 = (f.get("volatility") or {}).get("vol_60d_pct")
    ory = f.get("or_yoy") or {}
    # sustained-weak：多季（>=2）且未持续≥15% 且最新<15%（OV#2：多季连续弱，非单季 None）
    sustained_weak = (
        (ory.get("quarters_n", 0) >= 2)
        and not ory.get("sustained_high")
        and (ory.get("latest") is None or ory.get("latest") < 15)
    )

    def _apply_gate2(primary: Optional[str], reasons: list, low_conf: bool) -> bool:
        if primary == "长线" and vol60 is not None and vol60 > 80 and sustained_weak:
            reasons.append(f"⚠ 长线+高波动({vol60:.0f}%)+业绩多季持续弱，长线判定存疑（建议重判）")
            return True
        return low_conf

    if ai_playstyle and isinstance(ai_playstyle, dict) and ai_playstyle.get("ratings"):
        raw_ratings = ai_playstyle["ratings"] or {}
        ratings = {n: _clamp(raw_ratings.get(n, 0), 0, 5) for n in PLAYSTYLES}
        reasons = list(ai_playstyle.get("reasons") or [])
        primary = ai_playstyle.get("primary")
        # gate #1 (C2)：primary 必须 tied-for-max；不符自动改 argmax（软，不 reject）
        mx = max(ratings.values()) if ratings else 0
        if primary not in PLAYSTYLES or ratings.get(primary, 0) != mx:
            primary = max(PLAYSTYLES, key=lambda n: ratings.get(n, 0)) if ratings else None
            reasons.append(f"primary 自动对齐 argmax({primary})（声明与 ratings 不一致）")
        secondary = ai_playstyle.get("secondary")
        if secondary not in PLAYSTYLES or secondary == primary:
            secondary = _second_highest(ratings)
        low_conf = (mx <= 1)
        low_conf = _apply_gate2(primary, reasons, low_conf)
        return {"ratings": ratings, "primary": primary, "secondary": secondary,
                "reasons": reasons, "method": "ai_skill", "low_confidence": low_conf}

    # AI 未填 -> Python fallback（FE>=0.5 但 AI 没填 playstyle）
    rule = compute_rule_ratings(feats)
    if not rule.get("usable"):
        return None
    ratings = rule["ratings"]
    primary = rule["top"]
    secondary = _second_highest(ratings)
    reasons = list(rule.get("reasons") or [])
    reasons.append("AI 未填 playstyle，Python 规则 fallback")
    low_conf = _apply_gate2(primary, reasons, rule.get("low_confidence", False))
    return {"ratings": ratings, "primary": primary, "secondary": secondary,
            "reasons": reasons, "method": "python_fallback", "low_confidence": low_conf}
