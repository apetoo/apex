"""Playstyle Engine v1 单测（T8）。

覆盖：
- per_day_cache（T3）：去重 / dash 归一 / 空结果缓存 / clear/stats / 装饰器透明
- FE extract_features（T2）：10 特征 present / completeness / fail-soft(raises->0) / FE 缓存命中
- D11 换手率自推公式 / D12 多季 or_yoy（sustained_high + trend）
- compute_risk_level（D13）：low/medium/high
- compute_rule_ratings（T4）：4 原型 top + 打野/中线 cap4 / 波段/长线 5★ / 平局 / fail-soft / low_confidence
- compute_playstyle_fit（T6）：None->特征不足 / dict->画像累积中
- _format_playstyle_block：>0.5 与 <0.5 两路径
- journal lazy-fill（T5）：validate_entry + load_entries 补 pre-v9 字段

纯函数 + mock 数据层，不调 tushare/akshare/DeepSeek。
"""
import json
from unittest.mock import patch

import pytest

from apex import playstyle as P
from apex import analyze, journal
from apex.per_day_cache import per_day_cache, clear_cache, cache_stats


# ── 测试用 mock 数据层 ──────────────────────────────────────────────────────────

def _mock_daily(*a, **k):
    """60 根 qfq 日线，温和上行 + MA 多头排列。"""
    bars = []
    c = 10.0
    for i in range(60):
        c = c * (1 + 0.002 * ((-1) ** i) * 0.5 + 0.001)
        bars.append({
            "trade_date": f"2026{(i // 30) + 1:02d}{(i % 30) + 1:02d}",
            "open": c - 0.05, "high": c + 0.1, "low": c - 0.1, "close": round(c, 2),
            "vol": 100000 + i * 100,
            "ma5": round(c * 1.01, 2), "ma10": round(c * 1.005, 2),
            "ma20": round(c * 0.99, 2), "ma60": round(c * 0.95, 2),
            "vol_ratio": 1.1,
        })
    return json.dumps(bars)


def _mock_fund(ts_code, *, or_yoy=25.0, roe=12.0, pe_ttm=25.0, pb=3.0,
               circ_mv=300000, turnover_rate=7.0, quarters_n=4, trend="improving"):
    """fundamentals dict，可调业绩/估值参数。trend 由 or_yoy 序列决定。
    circ_mv 单位=万元（同 tushare daily_basic 原值，extract_features 内 ×1e4 转元）。300000 万元=30 亿。"""
    seq = {"improving": [25, 18, 20, 16], "declining": [5, 12, 18, 20],
           "stable": [20, 19, 21, 20]}.get(trend, [25, 18, 20, 16])[:quarters_n]
    roe_seq = [roe, roe - 1, roe - 1.5, roe - 2][:quarters_n]
    quarters = [{"end_date": f"2026Q{i+1}", "or_yoy": seq[i], "roe": roe_seq[i]} for i in range(len(seq))]
    return json.dumps({
        "valuation": {"trade_date": "20260714", "pe_ttm": pe_ttm, "pb": pb,
                      "circ_mv": circ_mv, "turnover_rate": turnover_rate},
        "quarters": quarters,
        "summary": {"data_quarters": quarters_n, "flags": []},
        "latest_quarter": "2026Q1",
    })


def _mock_moneyflow_positive(d):
    return json.dumps([{"ts_code": "603019.SH", "trade_date": d, "net_mf_amount": 8.0e7, "source": "tushare"}])


def _mock_northbound_listed(d):
    return [{"ts_code": "603019.SH", "name": "x", "signal_type": "northbound",
             "signal_strength": 0.5, "raw": {"inflow_mv": 1.2e8}}]


def _mock_limit_up_hit(d):
    if d in ("20260714", "20260713", "20260712"):
        return [{"ts_code": "603019.SH", "name": "x", "signal_type": "limit_up",
                 "signal_strength": 0.8,
                 "raw": {"limit_times": 3 if d == "20260714" else 2}}]
    return []


def _patch_data(daily=_mock_daily, fund=_mock_fund, mf=_mock_moneyflow_positive,
                north=_mock_northbound_listed, limit=_mock_limit_up_hit,
                last_n=lambda *a, **k: ["20260714", "20260713", "20260712", "20260711", "20260710"]):
    """一次性 patch FE 依赖的 data + signals。返回 contextmanager 列表用 with。"""
    import apex.signals.northbound as nb
    import apex.signals.limit_up as lu
    return [
        patch.object(P.data, "get_daily_price", daily),
        patch.object(P.data, "get_fundamentals", fund),
        patch.object(P.data, "get_moneyflow", mf),
        patch.object(P.data, "last_n_trade_dates", last_n),
        patch.object(nb, "fetch", north),
        patch.object(lu, "fetch", limit),
    ]


def _run_patches(patches, fn):
    """逐个 enter patches 后跑 fn，最后 exit。"""
    ctxs = [p.__enter__() for p in patches]
    try:
        return fn()
    finally:
        for p, _ in zip(patches, ctxs):
            p.__exit__(None, None, None)


def _feats(**fund_kwargs):
    """提取 603019.SH 的特征（mock 数据）。fund_kwargs 透传给 _mock_fund。"""
    P.clear_fe_cache()
    patches = _patch_data(fund=lambda ts: _mock_fund(ts, **fund_kwargs))
    return _run_patches(patches, lambda: P.extract_features("603019.SH", as_of="20260714"))


def _raise(*a, **k):
    raise RuntimeError("network down")


# ── per_day_cache（T3）──────────────────────────────────────────────────────────

class TestPerDayCache:
    def setup_method(self):
        clear_cache()

    def test_same_date_dedup_with_dash_normalization(self):
        calls = {"n": 0}

        @per_day_cache("t")
        def f(trade_date):
            calls["n"] += 1
            return [{"d": (trade_date or "").replace("-", "")}]

        assert f("20260714") == [{"d": "20260714"}]
        assert f("20260714-") == [{"d": "20260714"}]  # dash 归一，命中缓存
        assert calls["n"] == 1

    def test_different_date_miss(self):
        calls = {"n": 0}

        @per_day_cache("t2")
        def f(trade_date):
            calls["n"] += 1
            return []

        f("20260714")
        f("20260713")
        assert calls["n"] == 2

    def test_empty_result_cached(self):
        calls = {"n": 0}

        @per_day_cache("t3")
        def f(trade_date):
            calls["n"] += 1
            return []

        f("20260712")
        f("20260712")
        assert calls["n"] == 1

    def test_clear_and_stats(self):
        @per_day_cache("t4")
        def f(trade_date):
            return []

        f("20260714")
        f("20260713")
        assert cache_stats().get("t4") == 2
        assert clear_cache("t4") == 2
        assert "t4" not in cache_stats()
        clear_cache()

    def test_decorator_transparent_for_fetchers(self):
        """装饰后 FETCHERS 仍引用同一函数，__name__ 保留。"""
        from apex.signals import FETCHERS, moneyflow, northbound, limit_up
        assert FETCHERS["moneyflow"] is moneyflow.fetch
        assert FETCHERS["northbound"] is northbound.fetch
        assert FETCHERS["limit_up"] is limit_up.fetch
        assert limit_up.fetch.__name__ == "fetch"
        assert moneyflow.fetch._per_day_cache_name == "moneyflow"


# ── FE extract_features（T2）────────────────────────────────────────────────────

class TestExtractFeatures:
    def test_ten_features_present_full_data(self):
        feats = _feats()
        assert feats["completeness"] == 1.0
        for name in P.FEATURE_NAMES:
            assert feats["features"][name].get("present"), name

    def test_d11_turnover_self_derivation_and_daily_basic_override(self):
        """avg_20d_pct 自推 > 0；latest_pct 被 daily_basic turnover_rate 覆盖。"""
        feats = _feats(turnover_rate=4.2)
        tr = feats["features"]["turnover"]
        assert tr["present"]
        assert tr["avg_20d_pct"] > 0
        assert tr["latest_pct"] == 4.2
        assert tr["latest_source"] == "daily_basic"

    def test_d11_turnover_formula_unit(self):
        """vol*close*10000/circ_mv_yuan = %（手->股 ×100 + 分数->% ×100）。circ_mv 单位万元。"""
        # 30 亿 circ_mv = 300000 万元 -> yuan=3e9；vol=100000手 close~10 -> ~3.3%
        feats = _feats(circ_mv=300000)
        # avg_20d_pct 应在合理量级（不会是 0.000x 也不会是 400）
        avg = feats["features"]["turnover"]["avg_20d_pct"]
        assert 0.1 < avg < 100, avg

    def test_d12_multi_quarter_or_yoy_sustained_high(self):
        feats = _feats(or_yoy=25.0, trend="improving")
        o = feats["features"]["or_yoy"]
        assert o["latest"] == 25.0
        assert o["sustained_high"] is True  # 4 季全 >=15
        assert o["trend"] == "improving"
        assert o["quarters_n"] == 4

    def test_d12_declining_trend(self):
        feats = _feats(trend="declining")
        assert feats["features"]["or_yoy"]["trend"] == "declining"

    def test_fail_soft_all_raise_completeness_zero(self):
        """所有数据源抛异常 -> completeness 0.0，不 crash。"""
        P.clear_fe_cache()
        patches = _patch_data(daily=_raise, fund=_raise, mf=_raise, north=_raise,
                              limit=_raise, last_n=lambda *a, **k: [])
        feats = _run_patches(patches, lambda: P.extract_features("000001.SZ", as_of="20260714"))
        assert feats["completeness"] == 0.0
        assert all(not feats["features"][n].get("present") for n in P.FEATURE_NAMES)

    def test_fail_soft_partial_completeness_below_half(self):
        """仅 fundamentals 可用 -> 4 特征 present -> completeness 0.4 (<0.5)。"""
        P.clear_fe_cache()
        patches = _patch_data(daily=_raise, mf=_raise, north=_raise, limit=_raise,
                              last_n=lambda *a, **k: [])
        feats = _run_patches(patches, lambda: P.extract_features("000001.SZ", as_of="20260714"))
        present = [n for n in P.FEATURE_NAMES if feats["features"][n].get("present")]
        assert present == ["or_yoy", "circ_mv", "valuation", "roe"]
        assert feats["completeness"] == 0.4

    def test_fe_cache_hit_no_recompute(self):
        _feats()  # populate
        # 第二次同 ts_code+as_of 应命中缓存（同对象）
        feats1 = P.extract_features("603019.SH", as_of="20260714")
        P.clear_fe_cache()  # 清后重算会不同对象
        assert feats1 is not None
        # 重置缓存后再取，确认缓存确实在起作用：不清缓存时两次同对象
        _feats()
        a = P.extract_features("603019.SH", as_of="20260714")
        b = P.extract_features("603019.SH", as_of="20260714")
        assert a is b

    def test_ma_alignment_perfect_bullish(self):
        feats = _feats()
        # mock_daily 的 ma5>ma10>ma20>ma60 -> perfect
        assert feats["features"]["ma_alignment"]["perfect_bullish"] is True
        assert feats["features"]["ma_alignment"]["score"] == 3

    def test_risk_level_field_present(self):
        feats = _feats()
        assert feats["risk_level"] in ("low", "medium", "high")


# ── compute_risk_level（D13）─────────────────────────────────────────────────────

class TestRiskLevel:
    def test_low(self):
        f = {"volatility": {"vol_60d_pct": 20}, "circ_mv": {"yi": 300},
             "or_yoy": {"latest": 25, "trend": "improving"}}
        assert P.compute_risk_level(f) == "low"

    def test_high_volatility(self):
        f = {"volatility": {"vol_60d_pct": 80}, "circ_mv": {"yi": 300}, "or_yoy": {"latest": 25}}
        assert P.compute_risk_level(f) == "high"

    def test_high_small_cap(self):
        f = {"volatility": {"vol_60d_pct": 20}, "circ_mv": {"yi": 30}, "or_yoy": {"latest": 25}}
        assert P.compute_risk_level(f) == "high"

    def test_high_declining_earnings(self):
        f = {"volatility": {"vol_60d_pct": 20}, "circ_mv": {"yi": 300},
             "or_yoy": {"latest": 5, "trend": "declining"}}
        assert P.compute_risk_level(f) == "high"

    def test_medium(self):
        f = {"volatility": {"vol_60d_pct": 40}, "circ_mv": {"yi": 100},
             "or_yoy": {"latest": 5, "trend": "stable"}}
        assert P.compute_risk_level(f) == "medium"

    def test_missing_features_default_medium(self):
        assert P.compute_risk_level({}) == "medium"


# ── compute_rule_ratings（T4）────────────────────────────────────────────────────

def _feat_dict(**overrides):
    """构造内层 features dict（compute_rule_ratings 消费）。"""
    base = {k: {"present": True} for k in P.FEATURE_NAMES}
    base.update({
        "limit_up": {"max_consecutive_10d": 0, "count_10d": 0, "present": True},
        "turnover": {"avg_20d_pct": 5.0, "present": True},
        "ma_alignment": {"score": 0, "perfect_bullish": False, "present": True},
        "moneyflow": {"sign": 0, "consec_positive": 0, "present": True},
        "or_yoy": {"latest": 0, "trend": "stable", "sustained_high": False, "present": True},
        "valuation": {"pe_ttm": 50, "pb": 5, "present": True},
        "roe": {"latest": 0, "all_positive_4q": False, "present": True},
        "volatility": {"vol_20d_pct": 40, "vol_60d_pct": 40, "present": True},
    })
    base.update(overrides)
    return base


def _wrap(feat_dict, completeness=1.0):
    return {"features": feat_dict, "completeness": completeness, "risk_level": "medium"}


class TestRuleRatings:
    def test_daye_archetype_capped_at_4(self):
        """打野 5★ 题材龙头需 AI -> Python cap 4。"""
        f = _feat_dict(limit_up={"max_consecutive_10d": 3, "count_10d": 3, "present": True},
                       turnover={"avg_20d_pct": 18.0, "present": True},
                       or_yoy={"latest": -5, "trend": "declining", "sustained_high": False, "present": True})
        r = P.compute_rule_ratings(_wrap(f))
        assert r["top"] == "打野"
        assert r["ratings"]["打野"] == 4  # capped
        assert r["ratings"]["打野"] <= 4
        assert any("连板" in x for x in r["reasons"])

    def test_band_archetype_can_reach_5(self):
        """波段 5★ 全 FE 可达。"""
        f = _feat_dict(ma_alignment={"score": 3, "perfect_bullish": True, "present": True},
                       moneyflow={"sign": 1, "consec_positive": 5, "present": True},
                       volatility={"vol_20d_pct": 30, "vol_60d_pct": 32, "present": True},
                       turnover={"avg_20d_pct": 7.0, "present": True},
                       limit_up={"max_consecutive_10d": 0, "count_10d": 0, "present": True})
        r = P.compute_rule_ratings(_wrap(f))
        assert r["top"] == "波段"
        assert r["ratings"]["波段"] == 5

    def test_mid_archetype_capped_at_4(self):
        """中线 5★ 行业景气需 AI -> cap 4。"""
        f = _feat_dict(or_yoy={"latest": 25, "trend": "improving", "sustained_high": True, "present": True},
                       valuation={"pe_ttm": 25, "pb": 3, "present": True},
                       roe={"latest": 12, "all_positive_4q": True, "present": True})
        r = P.compute_rule_ratings(_wrap(f))
        assert r["top"] == "中线"
        assert r["ratings"]["中线"] <= 4

    def test_long_archetype_can_reach_5(self):
        """长线 5★ 全 FE 可达。"""
        f = _feat_dict(or_yoy={"latest": 16, "trend": "stable", "sustained_high": True, "present": True},
                       valuation={"pe_ttm": 15, "pb": 1.2, "present": True},
                       roe={"latest": 13, "all_positive_4q": True, "present": True},
                       ma_alignment={"score": 0, "perfect_bullish": False, "present": True},
                       volatility={"vol_20d_pct": 25, "vol_60d_pct": 28, "present": True})
        r = P.compute_rule_ratings(_wrap(f))
        assert r["top"] == "长线"
        assert r["ratings"]["长线"] == 5

    def test_declining_earnings_caps_mid_long_at_1(self):
        f = _feat_dict(or_yoy={"latest": 30, "trend": "declining", "sustained_high": True, "present": True},
                       valuation={"pe_ttm": 15, "pb": 1.2, "present": True},
                       roe={"latest": 13, "all_positive_4q": True, "present": True})
        r = P.compute_rule_ratings(_wrap(f))
        assert r["ratings"]["中线"] <= 1
        assert r["ratings"]["长线"] <= 1

    def test_completeness_below_half_usable_false(self):
        r = P.compute_rule_ratings(_wrap(_feat_dict(), completeness=0.4))
        assert r["usable"] is False
        assert r["ratings"] is None
        assert r["top"] is None

    def test_all_zero_low_confidence(self):
        # 全弱特征：vol>80(波段不适中) / pe>80(中线估值高) / turnover<5 / 业绩0
        f = _feat_dict(volatility={"vol_20d_pct": 90, "vol_60d_pct": 95, "present": True},
                       valuation={"pe_ttm": 200, "pb": 10, "present": True},
                       turnover={"avg_20d_pct": 2.0, "present": True})
        r = P.compute_rule_ratings(_wrap(f))
        assert r["low_confidence"] is True
        assert all(v == 0 for v in r["ratings"].values()), r["ratings"]

    def test_tie_break_first_and_documented(self):
        """平局取首个并在 reasons 说明。"""
        # 中线/长线 都 4★（同或_yoy/ROE/估值命中）
        f = _feat_dict(or_yoy={"latest": 25, "trend": "improving", "sustained_high": True, "present": True},
                       valuation={"pe_ttm": 25, "pb": 3, "present": True},
                       roe={"latest": 13, "all_positive_4q": True, "present": True})
        r = P.compute_rule_ratings(_wrap(f))
        mx = max(r["ratings"].values())
        tied = [k for k in P.PLAYSTYLES if r["ratings"][k] == mx]
        if len(tied) > 1:
            assert any("平局" in x for x in r["reasons"])

    def test_evaluate_benchmark_d9_branch(self):
        """evaluate_benchmark 返回 hit_rate + d9_branch（mock extract_features）。"""
        bench = [{"ts_code": "603019.SH", "expected_top": "波段", "why": "趋势完整"}]
        f = _feat_dict(ma_alignment={"score": 3, "perfect_bullish": True, "present": True},
                       moneyflow={"sign": 1, "consec_positive": 5, "present": True},
                       volatility={"vol_20d_pct": 30, "vol_60d_pct": 32, "present": True},
                       turnover={"avg_20d_pct": 7.0, "present": True})
        with patch.object(P, "extract_features", lambda ts, as_of=None: _wrap(f)):
            rep = P.evaluate_benchmark(bench)
        assert rep["n"] == 1
        assert rep["hit_rate"] == 1.0
        assert "python_rule" in rep["d9_branch"]


# ── compute_playstyle_fit（T6）───────────────────────────────────────────────────

class TestPlaystyleFit:
    def test_none_means_feature_insufficient(self):
        fit = P.compute_playstyle_fit(None)
        assert fit["state"] == "insufficient_data"
        assert "特征不足" in fit["note"]

    def test_dict_means_profile_accumulating(self):
        fit = P.compute_playstyle_fit({"top": "长线", "ratings": {}})
        assert fit["state"] == "insufficient_data"
        assert "画像累积中" in fit["note"]

    def test_v1_always_insufficient_data(self):
        """v1 (D10) 不论输入都 insufficient_data，无 matched/mismatch。"""
        for inp in [None, {"top": "打野"}, {"top": "波段"}]:
            assert P.compute_playstyle_fit(inp)["state"] == "insufficient_data"


# ── finalize_playstyle（skill 分支：AI 填 + 软门控）──────────────────────────────

def _feats_wrap(features: dict, completeness=1.0):
    return {"features": features, "completeness": completeness, "risk_level": "medium",
            "ts_code": "x", "as_of": "20260714", "notes": []}


class TestFinalizePlaystyle:
    def test_fe_below_half_returns_none(self):
        """FE completeness<0.5 -> None（playstyle=null 路径），不管 AI 填没填。"""
        feats = _feats_wrap({}, completeness=0.4)
        assert P.finalize_playstyle({"ratings": {"打野": 5}, "primary": "打野"}, feats) is None

    def test_ai_valid_primary_kept(self):
        """AI 填的 primary = argmax -> 保留，method=ai_skill。"""
        f = _feat_dict()
        feats = _feats_wrap(f)
        ai = {"ratings": {"打野": 4, "波段": 2, "中线": 1, "长线": 0}, "primary": "打野",
              "secondary": "波段", "reasons": ["游资属性(连板3)"]}
        out = P.finalize_playstyle(ai, feats)
        assert out["method"] == "ai_skill"
        assert out["primary"] == "打野"
        assert out["secondary"] == "波段"
        assert out["ratings"]["打野"] == 4

    def test_gate1_primary_not_argmax_autofixed(self):
        """gate#1 (C2)：primary 不是 tied-for-max -> 自动改成 argmax（软，不 reject）。"""
        f = _feat_dict()
        feats = _feats_wrap(f)
        # AI 说 primary=长线(0★) 但打野(4★) 最高 -> 自动改成打野
        ai = {"ratings": {"打野": 4, "波段": 2, "中线": 1, "长线": 0}, "primary": "长线",
              "reasons": []}
        out = P.finalize_playstyle(ai, feats)
        assert out["primary"] == "打野"
        assert any("自动对齐 argmax" in r for r in out["reasons"])

    def test_gate1_tied_for_max_allowed(self):
        """C2：平局时 primary 是并列最高之一即合法（不强制 argmax 首个）。"""
        f = _feat_dict()
        feats = _feats_wrap(f)
        ai = {"ratings": {"打野": 4, "波段": 4, "中线": 0, "长线": 0}, "primary": "波段",
              "reasons": []}
        out = P.finalize_playstyle(ai, feats)
        assert out["primary"] == "波段"  # 并列最高，保留 AI 选择
        assert "自动对齐" not in " ".join(out["reasons"])

    def test_secondary_derived_when_missing(self):
        """AI 没填 secondary -> 自动取次高。"""
        f = _feat_dict()
        feats = _feats_wrap(f)
        ai = {"ratings": {"打野": 4, "波段": 3, "中线": 1, "长线": 0}, "primary": "打野",
              "reasons": []}
        out = P.finalize_playstyle(ai, feats)
        assert out["secondary"] == "波段"  # 次高

    def test_gate2_long_highvol_sustained_weak_flags(self):
        """gate#2 (OV#2)：primary=长线 + vol60>80 + 多季持续弱 -> low_confidence + 原因。"""
        f = _feat_dict(
            volatility={"vol_20d_pct": 90, "vol_60d_pct": 95, "present": True},
            or_yoy={"latest": 8, "trend": "stable", "sustained_high": False, "quarters_n": 4, "present": True},
        )
        feats = _feats_wrap(f)
        ai = {"ratings": {"打野": 0, "波段": 1, "中线": 2, "长线": 4}, "primary": "长线",
              "reasons": ["护城河(web_search)"]}
        out = P.finalize_playstyle(ai, feats)
        assert out["primary"] == "长线"
        assert out["low_confidence"] is True
        assert any("长线+高波动" in r for r in out["reasons"])

    def test_ai_not_filled_python_fallback(self):
        """AI 未填 playstyle 但 FE>=0.5 -> Python compute_rule_ratings fallback。"""
        f = _feat_dict(ma_alignment={"score": 3, "perfect_bullish": True, "present": True},
                       moneyflow={"sign": 1, "consec_positive": 5, "present": True},
                       volatility={"vol_20d_pct": 30, "vol_60d_pct": 32, "present": True},
                       turnover={"avg_20d_pct": 7.0, "present": True})
        feats = _feats_wrap(f)
        out = P.finalize_playstyle(None, feats)
        assert out["method"] == "python_fallback"
        assert out["primary"] == "波段"
        assert "fallback" in " ".join(out["reasons"])

    def test_ratings_clamped_0_5(self):
        """AI 填越界星级 -> clamp 到 0-5。"""
        f = _feat_dict()
        feats = _feats_wrap(f)
        ai = {"ratings": {"打野": 99, "波段": -3, "中线": 3, "长线": 0}, "primary": "打野",
              "reasons": []}
        out = P.finalize_playstyle(ai, feats)
        assert out["ratings"]["打野"] == 5
        assert out["ratings"]["波段"] == 0


# ── _format_playstyle_block（T2 analyze 侧）─────────────────────────────────────

class TestFormatBlock:
    def test_above_half_renders_features(self):
        P.clear_fe_cache()
        patches = _patch_data()
        block, raw = _run_patches(patches, lambda: analyze._format_playstyle_block("603019.SH"))
        assert isinstance(block, str) and isinstance(raw, dict)
        assert "玩法特征" in block
        assert raw["completeness"] >= 0.5
        assert "降级为 null" not in block

    def test_below_half_renders_degradation(self):
        P.clear_fe_cache()
        patches = _patch_data(daily=_raise, mf=_raise, north=_raise, limit=_raise,
                              last_n=lambda *a, **k: [])
        block, raw = _run_patches(patches, lambda: analyze._format_playstyle_block("000001.SZ"))
        assert "降级为 null" in block
        assert raw["completeness"] < 0.5


# ── journal lazy-fill（T5）───────────────────────────────────────────────────────

class TestJournalLazyFill:
    def test_validate_entry_fills_playstyle_fields(self):
        old = {"ts_code": "603019.SH", "verdict": "看多", "features": {"ma5_position": "above"}}
        journal.validate_entry(old)
        for k in ("playstyle", "playstyle_fit", "playstyle_features", "risk_level"):
            assert k in old and old[k] is None

    def test_validate_entry_preserves_existing_playstyle(self):
        e = {"ts_code": "603019.SH", "verdict": "看多", "features": {},
             "playstyle": {"top": "长线"}, "risk_level": "low"}
        journal.validate_entry(e)
        assert e["playstyle"] == {"top": "长线"}
        assert e["risk_level"] == "low"
        # 其余仍补 None
        assert e["playstyle_fit"] is None

    def test_load_entries_fills_old_preserves_new(self, tmp_path, monkeypatch):
        jdir = tmp_path / "journal"
        jdir.mkdir()
        monkeypatch.setattr(journal, "_journal_dir", lambda: jdir)
        p = jdir / "603019.SH.jsonl"
        p.write_text(
            json.dumps({"ts_code": "603019.SH", "verdict": "看多", "features": {}, "date": "2026-07-01"}) + "\n"
            + json.dumps({"ts_code": "603019.SH", "verdict": "偏多", "features": {}, "date": "2026-07-10",
                          "playstyle": {"top": "长线"}, "playstyle_fit": {"state": "insufficient_data"},
                          "playstyle_features": {"completeness": 0.8}, "risk_level": "low"}) + "\n"
        )
        entries = journal.load_entries("603019.SH")
        assert len(entries) == 2
        assert entries[0]["playstyle"] is None and entries[0]["risk_level"] is None
        assert entries[1]["playstyle"]["top"] == "长线"
        assert entries[1]["risk_level"] == "low"
