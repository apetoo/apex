"""screener_backtest 因子评测层单测（项目首个测试套，工程评审 T6）。

覆盖纯函数（_spearman_ic / _aggregate_ic / _pool_alpha / _verdict）+ _simulate
多 horizon 形状回归（mock market_cache，无 API）。

关键回归（CRITICAL）：_simulate(return_gross=False) 输出形状与改动前字节一致
（胜率路径零行为变更）。_simulate(return_gross=True) 多 horizon gross/net 独立锚。
"""
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest

from apex import screener_backtest as sb


# ── _spearman_ic ───────────────────────────────────────────────────────────────

def test_spearman_ic_perfect_positive():
    # score 与 return 完全单调递增 → IC=1.0
    scores = [1.0, 2.0, 3.0, 4.0, 5.0]
    rets = [0.01, 0.02, 0.03, 0.04, 0.05]
    assert sb._spearman_ic(scores, rets) == 1.0


def test_spearman_ic_perfect_negative():
    scores = [1.0, 2.0, 3.0, 4.0, 5.0]
    rets = [0.05, 0.04, 0.03, 0.02, 0.01]
    assert sb._spearman_ic(scores, rets) == -1.0


def test_spearman_ic_too_few_samples():
    assert sb._spearman_ic([1.0, 2.0], [0.1, 0.2]) is None


def test_spearman_ic_tied_scores_rejected():
    # unique score < 3 → 无排序力 → None（设计守卫）
    assert sb._spearman_ic([1.0, 1.0, 1.0, 1.0], [0.1, 0.2, 0.3, 0.4]) is None


def test_spearman_ic_no_return_variance():
    # return 全相同 → 无方差 → None
    assert sb._spearman_ic([1.0, 2.0, 3.0, 4.0], [0.1, 0.1, 0.1, 0.1]) is None


def test_spearman_ic_ties_average_rank():
    # 带 ties 的手算验证：scores=[1,2,2,3], rets 单调 → 应为正且 <1
    ic = sb._spearman_ic([1.0, 2.0, 2.0, 3.0, 4.0], [0.01, 0.02, 0.025, 0.03, 0.04])
    assert ic is not None
    assert 0.8 < ic <= 1.0


# ── _aggregate_ic（n_dates 守卫 + per-date ≥5 & ≥3 unique）────────────────────

def _row(date, score, gross_5d):
    return {
        "signal_date": date,
        "strategy_score": score,
        "sim": {"unfillable": False, "gross": {1: 0.0, 5: gross_5d, 10: 0.0}},
    }


def test_aggregate_ic_n_dates_guard():
    # 7 个日期各 5 行 → n_dates=7 < 8 → 仍聚合出 ic_mean（守卫在 verdict，不在聚合）
    # 聚合本身只看 per-date 守卫；n_dates<8 由 _verdict 判 n_insufficient
    rows = []
    for d in range(7):
        for i in range(5):
            rows.append(_row(f"2026-01-{d+1:02d}", float(i), float(i) * 0.01))
    agg = sb._aggregate_ic(rows, 5)
    assert agg["n_dates"] == 7
    assert agg["ic_mean"] == 1.0  # 每日完美单调


def test_aggregate_ic_drops_date_with_fewer_than_5():
    # 一个日期仅 4 行 → 不进分母
    rows = []
    for d in range(3):
        for i in range(5):
            rows.append(_row(f"2026-01-{d+1:02d}", float(i), float(i) * 0.01))
    for i in range(4):  # 第 4 日仅 4 行
        rows.append(_row("2026-01-04", float(i), float(i) * 0.01))
    agg = sb._aggregate_ic(rows, 5)
    assert agg["n_dates"] == 3  # 第 4 日被剔


def test_aggregate_ic_drops_tied_score_date():
    # 一个日期 5 行但 score 全相同（unique<3）→ 不进分母
    rows = []
    for d in range(3):
        for i in range(5):
            rows.append(_row(f"2026-01-{d+1:02d}", float(i), float(i) * 0.01))
    for i in range(5):
        rows.append(_row("2026-01-04", 1.0, float(i) * 0.01))  # 全 tied
    agg = sb._aggregate_ic(rows, 5)
    assert agg["n_dates"] == 3


def test_aggregate_ic_empty():
    assert sb._aggregate_ic([], 5) == {
        "ic_mean": None, "ic_std": None, "n_dates": 0, "ir": None, "t_stat": None
    }


def test_aggregate_ic_ir_and_tstat():
    # 8 日，每日完美正单调 → ic_mean=1, ic_std=0 → ir/t_stat=None（除零保护）
    rows = []
    for d in range(8):
        for i in range(5):
            rows.append(_row(f"2026-01-{d+1:02d}", float(i), float(i) * 0.01))
    agg = sb._aggregate_ic(rows, 5)
    assert agg["n_dates"] == 8
    assert agg["ir"] is None  # std=0
    assert agg["t_stat"] is None


# ── _pool_alpha（成本对称：两侧 gross）─────────────────────────────────────────

def test_pool_alpha_gross_both_sides():
    rows = [
        _row("2026-01-01", 1.0, 0.03),  # gross 5d 3%
        _row("2026-01-01", 2.0, 0.05),  # gross 5d 5%
    ]
    bench = {"2026-01-01": 0.02}  # 基准 gross 2%
    pa = sb._pool_alpha(rows, bench)
    # (0.03-0.02 + 0.05-0.02)/2 = 0.02
    assert pa["pool_alpha"] == pytest.approx(0.02, abs=1e-6)
    assert pa["benchmark_n"] == 2
    assert pa["benchmark_missing_count"] == 0


def test_pool_alpha_missing_benchmark_excluded():
    rows = [
        _row("2026-01-01", 1.0, 0.03),
        _row("2026-01-02", 2.0, 0.05),  # 该日无基准
    ]
    bench = {"2026-01-01": 0.02, "2026-01-02": None}
    pa = sb._pool_alpha(rows, bench)
    assert pa["pool_alpha"] == pytest.approx(0.01, abs=1e-6)  # 仅 (0.03-0.02)
    assert pa["benchmark_n"] == 1
    assert pa["benchmark_missing_count"] == 1


def test_pool_alpha_all_missing():
    rows = [_row("2026-01-01", 1.0, 0.03)]
    pa = sb._pool_alpha(rows, {"2026-01-01": None})
    assert pa["pool_alpha"] is None
    assert pa["benchmark_n"] == 0
    assert pa["benchmark_missing_count"] == 1


# ── _verdict（4 态优先级）──────────────────────────────────────────────────────

def test_verdict_n_insufficient_fillable():
    # fillable_n<10 → n_insufficient（即便 n_dates 够、基准够）
    assert sb._verdict(fillable_n=9, n_dates_5d=10, benchmark_n=10,
                       ic_5d=0.5, pool_alpha_5d=0.05) == "n_insufficient"


def test_verdict_n_insufficient_ndates():
    # n_dates<8 → n_insufficient（外音 #2 守卫，即便 fillable 够）
    assert sb._verdict(fillable_n=100, n_dates_5d=7, benchmark_n=10,
                       ic_5d=0.5, pool_alpha_5d=0.05) == "n_insufficient"


def test_verdict_alpha_unavailable():
    # n 够但 benchmark_n<5 → alpha_unavailable（含 industry_rotation）
    assert sb._verdict(fillable_n=50, n_dates_5d=10, benchmark_n=4,
                       ic_5d=0.5, pool_alpha_5d=None) == "alpha_unavailable"


def test_verdict_dead_weight():
    # 两项都弱 → dead_weight
    assert sb._verdict(fillable_n=50, n_dates_5d=10, benchmark_n=10,
                       ic_5d=0.03, pool_alpha_5d=0.005) == "dead_weight"


def test_verdict_live_candidate():
    assert sb._verdict(fillable_n=50, n_dates_5d=10, benchmark_n=10,
                       ic_5d=0.3, pool_alpha_5d=0.05) == "live_candidate"


def test_verdict_precedence_n_insufficient_over_alpha():
    # 同时 n_insufficient 和 alpha_unavailable 条件满足 → n_insufficient 优先
    assert sb._verdict(fillable_n=5, n_dates_5d=3, benchmark_n=0,
                       ic_5d=None, pool_alpha_5d=None) == "n_insufficient"


def test_verdict_dead_weight_needs_both_weak():
    # IC 弱但 alpha 强 → 不是 dead_weight
    assert sb._verdict(fillable_n=50, n_dates_5d=10, benchmark_n=10,
                       ic_5d=0.03, pool_alpha_5d=0.05) == "live_candidate"


# ── _simulate（mock market_cache，无 API）──────────────────────────────────────

def _make_df(rows):
    """rows: list of (open, high, low, close)。index=升序 trade_date。"""
    idx = [datetime(2026, 1, d) for d in range(1, len(rows) + 1)]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def test_simulate_return_gross_false_byte_identical_shape():
    """CRITICAL: return_gross=False 输出形状与改动前一致（胜率路径零变更）。"""
    # row0=信号日, row1=T+1 fill, 10 天后出场
    rows = [(10, 11, 9, 10)] + [(10, 11, 9, 10) for _ in range(11)]
    rows[1] = (10.5, 11, 10, 10.8)  # T+1: open 10.5
    rows[11] = (10, 11, 9, 12)      # T+11 exit close 12
    with patch("apex.market_cache.load_daily_full", return_value=_make_df(rows)):
        res = sb._simulate("600001.SH", "2026-01-01", 10, return_gross=False)
    # 旧契约：{net_return, hit, unfillable, exit_reason, fill_date, exit_date}
    assert set(res.keys()) == {"net_return", "hit", "unfillable", "exit_reason", "fill_date", "exit_date"}
    assert res["unfillable"] is False
    assert res["exit_reason"] == "time_stop"
    assert res["fill_date"] == "2026-01-02"
    # gross = (12 - 10.5)/10.5；net = gross - round_trip_cost
    gross = (12 - 10.5) / 10.5
    from apex import backtest as bt
    assert res["net_return"] == round(bt._apply_costs(gross), 4)


def test_simulate_return_gross_true_multihorizon():
    rows = [(10, 11, 9, 10)] + [(10, 11, 9, 10) for _ in range(13)]
    rows[1] = (10.0, 11, 9, 10.5)   # T+1 fill @ open 10.0
    rows[2] = (10, 11, 9, 10.1)     # T+1 close 10.1 → 1d
    rows[6] = (10, 11, 9, 11.0)     # T+5 close 11.0 → 5d
    rows[11] = (10, 11, 9, 12.0)    # T+10 close 12.0 → 10d
    with patch("apex.market_cache.load_daily_full", return_value=_make_df(rows)):
        res = sb._simulate("600001.SH", "2026-01-01", 10, return_gross=True)
    assert res["unfillable"] is False
    assert res["fill_price"] == 10.0
    assert res["gross"][1] == round((10.1 - 10.0) / 10.0, 6)
    assert res["gross"][5] == round((11.0 - 10.0) / 10.0, 6)
    assert res["gross"][10] == round((12.0 - 10.0) / 10.0, 6)
    # net = gross - round_trip_cost（成本对称：gross 剥成本）
    from apex import backtest as bt
    assert res["net"][5] == round(bt._apply_costs(res["gross"][5]), 6)


def test_simulate_unfillable_return_gross_false():
    # T+1 一字涨停 → unfillable
    rows = [(10, 11, 9, 10)] + [(11, 11, 11, 11)] + [(10, 11, 9, 10)] * 10
    with patch("apex.market_cache.load_daily_full", return_value=_make_df(rows)):
        res = sb._simulate("600001.SH", "2026-01-01", 10, return_gross=False)
    assert res["unfillable"] is True
    assert res["net_return"] is None
    assert res["exit_reason"] == "limit_unfillable"


def test_simulate_unfillable_return_gross_true():
    rows = [(10, 11, 9, 10)] + [(11, 11, 11, 11)] + [(10, 11, 9, 10)] * 10
    with patch("apex.market_cache.load_daily_full", return_value=_make_df(rows)):
        res = sb._simulate("600001.SH", "2026-01-01", 10, return_gross=True)
    assert res["unfillable"] is True
    assert all(v is None for v in res["gross"].values())


def test_simulate_data_insufficient_returns_none():
    with patch("apex.market_cache.load_daily_full", return_value=_make_df([(10, 11, 9, 10)])):
        assert sb._simulate("600001.SH", "2026-01-01", 10, return_gross=False) is None
