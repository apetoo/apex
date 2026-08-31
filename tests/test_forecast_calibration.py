import pytest

from apex import forecast_calibration as fc


@pytest.mark.parametrize(
    ("stock_return", "benchmark_return", "expected"),
    [(3.01, 2.0, "bull"), (3.0, 2.0, "neutral"), (0.99, 2.0, "bear"), (1.0, 2.0, "neutral")],
)
def test_excess_return_uses_one_percent_dead_zone(stock_return, benchmark_return, expected):
    outcome = fc.classify_outcome(stock_return, benchmark_return)

    assert outcome["excess_return_pct"] == pytest.approx(stock_return - benchmark_return)
    assert outcome["outcome"] == expected


@pytest.mark.parametrize(
    ("verdict", "outcome", "hit"),
    [
        ("看多", "bull", True), ("偏多", "neutral", False),
        ("观望偏多", "neutral", True), ("中性", "neutral", True),
        ("观望偏空", "neutral", True), ("偏空", "bear", True),
        ("看空", "bull", False),
    ],
)
def test_only_four_outer_verdicts_are_directional(verdict, outcome, hit):
    assert fc.is_forecast_hit(verdict, outcome) is hit


def _sample(hit: bool, verdict="偏多") -> dict:
    return {"verdict": verdict, "hit": hit, "policy_version": "decision-policy-v1"}


def test_ninth_sample_does_not_calibrate():
    result = fc.calibrate_confidence(7, "偏多", [_sample(False) for _ in range(9)])

    assert result.score == 7.0
    assert result.sample_size == 9
    assert result.applied is False


def test_tenth_sample_calibrates_once_from_forecast_hits():
    rows = [_sample(True) for _ in range(6)] + [_sample(False) for _ in range(4)]
    result = fc.calibrate_confidence(8, "偏多", rows)

    assert result.score == 6.0
    assert result.sample_size == 10
    assert result.applied is True


def test_trade_pnl_fields_do_not_affect_forecast_calibration():
    rows = [
        {**_sample(index < 5), "realized_pnl_pct": 99 if index >= 5 else -99}
        for index in range(10)
    ]

    result = fc.calibrate_confidence(8, "偏多", rows)

    assert result.score == 5.0


def test_legacy_policy_rows_are_not_calibration_samples():
    rows = [_sample(False) for _ in range(9)] + [{"verdict": "偏多", "hit": True}]

    result = fc.calibrate_confidence(7, "偏多", rows)

    assert result.sample_size == 9
    assert result.applied is False


def test_ten_trading_day_outcome_uses_matching_dates():
    stock = [
        {"trade_date": f"202608{day:02d}", "close": close}
        for day, close in [(3, 10), (4, 10.2), (5, 10.4), (6, 10.6), (7, 10.8),
                           (10, 11), (11, 11.2), (12, 11.4), (13, 11.6), (14, 11.8), (17, 12)]
    ]
    benchmark = [
        {"trade_date": row["trade_date"], "close": 100 + index}
        for index, row in enumerate(stock)
    ]

    outcome = fc.evaluate_matured_forecast(
        {"date": "2026-08-03", "verdict": "偏多", "policy_version": "decision-policy-v1"},
        stock,
        benchmark,
    )

    assert outcome["matured_at"] == "2026-08-17"
    assert outcome["stock_return_pct"] == pytest.approx(20.0)
    assert outcome["benchmark_return_pct"] == pytest.approx(10.0)
    assert outcome["excess_return_pct"] == pytest.approx(10.0)
    assert outcome["hit"] is True


def test_aggregate_only_accepts_completed_new_policy_entries():
    entries = [
        {"ts_code": "000001.SZ", "date": "2026-01-01", "analysis_status": "completed",
         "verdict": "偏多", "policy_version": "decision-policy-v1"},
        {"ts_code": "000002.SZ", "date": "2026-01-01", "analysis_status": "completed",
         "verdict": "看空"},
        {"ts_code": "000003.SZ", "date": "2026-01-01", "analysis_status": "insufficient_evidence",
         "verdict": "偏多", "policy_version": "decision-policy-v1"},
    ]
    days = [f"202601{day:02d}" for day in range(1, 12)]
    benchmark = [{"trade_date": day, "close": 100 + index} for index, day in enumerate(days)]
    stock = [{"trade_date": day, "close": 100 + index * 2} for index, day in enumerate(days)]

    rows = fc.aggregate_matured_forecasts(
        entries,
        stock_loader=lambda _code, _start, _end: stock,
        benchmark_loader=lambda _start, _end: benchmark,
        today="2026-02-01",
    )

    assert len(rows) == 1
    assert rows[0]["ts_code"] == "000001.SZ"
    assert rows[0]["benchmark_return_pct"] == pytest.approx(10.0)


def test_same_stock_same_day_counts_as_one_forecast_sample():
    entries = [
        {"ts_code": "000001.SZ", "date": "2026-01-01", "analyzed_at": f"2026-01-01T{hour}:00:00",
         "analysis_status": "completed", "verdict": verdict, "policy_version": "decision-policy-v1"}
        for hour, verdict in (("10", "看多"), ("15", "看空"))
    ]
    days = [f"202601{day:02d}" for day in range(1, 12)]
    bars = [{"trade_date": day, "close": 100 + index} for index, day in enumerate(days)]

    rows = fc.aggregate_matured_forecasts(
        entries, stock_loader=lambda *_args: bars,
        benchmark_loader=lambda *_args: bars, today="2026-02-01",
    )

    assert len(rows) == 1
    assert rows[0]["verdict"] == "看空"


def test_missing_stock_bar_on_benchmark_maturity_excludes_forecast():
    days = [f"202601{day:02d}" for day in range(1, 12)]
    benchmark = [{"trade_date": day, "close": 100 + index} for index, day in enumerate(days)]
    stock = [row for row in benchmark if row["trade_date"] != days[10]]

    outcome = fc.evaluate_matured_forecast(
        {"date": "2026-01-01", "verdict": "偏多", "policy_version": "decision-policy-v1"},
        stock, benchmark,
    )

    assert outcome is None
