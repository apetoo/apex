"""Regression tests for the signal-backtest execution contract."""

from unittest.mock import patch

import pandas as pd
import pytest

from apex import backtest as bt


def _bars(rows: list[tuple[float, float, float, float]], start: str = "2026-08-03") -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=["open", "high", "low", "close"],
        index=pd.bdate_range(start, periods=len(rows)),
    )


def _entry(*, stop_loss=None, target=None) -> dict:
    return {
        "ts_code": "600001.SH",
        "date": "2026-08-03",
        "verdict": "偏多",
        "confidence": 5,
        "price_advice": {"stop_loss": stop_loss, "target": target},
    }


@pytest.fixture(autouse=True)
def zero_costs(monkeypatch):
    monkeypatch.setattr(bt, "_apply_costs", lambda value: value)


def test_recent_short_window_is_pending_without_final_return():
    bars = _bars([(10, 10, 10, 10), (10, 10.5, 9.8, 10.2), (10.2, 10.4, 10, 10.3)])

    row = bt._simulate_one(
        _entry(), bars, holding_period=10, include_benchmark=False,
        as_of_date="2026-08-05",
    )

    assert row["status"] == "pending"
    assert row["exit_reason"] == "pending"
    assert row["exit_date"] is None
    assert row["net_return"] is None
    assert row["hit"] is None


def test_old_short_window_is_conservative_data_truncation():
    bars = _bars([(10, 10, 10, 10), (10, 10.5, 9.8, 10.2), (10.2, 10.4, 9.9, 9)])

    row = bt._simulate_one(
        _entry(), bars, holding_period=10, include_benchmark=False,
        as_of_date="2026-08-31",
    )

    assert row["status"] == "data_truncated"
    assert row["exit_reason"] == "data_truncated"
    assert row["exit_date"] == "2026-08-05"
    assert row["net_return"] == pytest.approx(-0.1)
    assert row["hit"] is False


def test_invalid_price_advice_is_not_flipped_into_stops():
    bars = _bars([
        (10, 10, 10, 10),
        (10, 10.5, 9.5, 10),
        (10, 11, 9, 10.5),
    ])

    row = bt._simulate_one(
        _entry(stop_loss=11, target=9), bars, holding_period=1,
        include_benchmark=False, as_of_date="2026-08-31",
    )

    assert row["invalid_price_advice"] is True
    assert row["exit_reason"] == "time_stop"
    assert row["net_return"] == pytest.approx(0.05)


@pytest.mark.parametrize("target", [9, float("nan"), float("inf")])
def test_price_advice_pair_is_atomic_and_finite(target):
    bars = _bars([
        (10, 10, 10, 10),
        (10, 10.2, 9.8, 10),
        (10, 10.5, 8, 10.4),
    ])

    row = bt._simulate_one(
        _entry(stop_loss=9, target=target), bars, holding_period=1,
        include_benchmark=False, as_of_date="2026-08-31",
    )

    assert row["invalid_price_advice"] is True
    assert row["exit_reason"] == "time_stop"
    assert row["net_return"] == pytest.approx(0.04)


def test_t_plus_one_forbids_exit_on_entry_day():
    bars = _bars([
        (10, 10, 10, 10),
        (10, 12, 8, 10.5),  # fill day touches both levels; cannot sell on T+1
        (10.5, 10.8, 10.2, 10.6),
    ])

    row = bt._simulate_one(
        _entry(stop_loss=9, target=11), bars, holding_period=1,
        include_benchmark=False, as_of_date="2026-08-31",
    )

    assert row["exit_reason"] == "time_stop"
    assert row["exit_date"] == "2026-08-05"
    assert row["net_return"] == pytest.approx(0.06)


def test_gap_through_stop_exits_at_open_not_stop_price():
    bars = _bars([
        (10, 10, 10, 10),
        (10, 10.3, 9.8, 10),
        (8, 8.5, 7.8, 8.2),
    ])

    row = bt._simulate_one(
        _entry(stop_loss=9, target=12), bars, holding_period=1,
        include_benchmark=False, as_of_date="2026-08-31",
    )

    assert row["exit_reason"] == "stop_hit"
    assert row["exit_price"] == 8
    assert row["net_return"] == pytest.approx(-0.2)


def test_same_bar_stop_and_target_uses_conservative_stop_first():
    bars = _bars([
        (10, 10, 10, 10),
        (10, 10.3, 9.8, 10),
        (10, 12, 8, 10.5),
    ])

    row = bt._simulate_one(
        _entry(stop_loss=9, target=11), bars, holding_period=1,
        include_benchmark=False, as_of_date="2026-08-31",
    )

    assert row["exit_reason"] == "stop_hit"
    assert row["exit_price"] == 9
    assert row["net_return"] == pytest.approx(-0.1)


def test_unresolved_limit_down_lock_remains_pending_without_fake_fill():
    bars = _bars([
        (10, 10, 10, 10),
        (10, 10.2, 9.8, 10),
        (9, 9, 9, 9),
    ])

    row = bt._simulate_one(
        _entry(stop_loss=9.5, target=12), bars, holding_period=10,
        include_benchmark=False, as_of_date="2026-08-05",
    )

    assert row["status"] == "pending"
    assert row["exit_reason"] == "stop_hit_limit_locked"
    assert row["exit_price"] is None
    assert row["net_return"] is None


def test_mature_window_still_cannot_fake_exit_on_locked_limit_down():
    bars = _bars([
        (10, 10, 10, 10),
        (10, 10.2, 9.8, 10),
        (9, 9, 9, 9),
    ])

    row = bt._simulate_one(
        _entry(stop_loss=9.5, target=12), bars, holding_period=1,
        include_benchmark=False, as_of_date="2026-08-31",
    )

    assert row["status"] == "pending"
    assert row["exit_reason"] == "stop_hit_limit_locked"
    assert row["exit_price"] is None
    assert row["net_return"] is None


def test_target_hit_return_direction_is_consistent():
    bars = _bars([
        (10, 10, 10, 10),
        (10, 10.3, 9.8, 10),
        (10.2, 11.5, 10.1, 11.2),
    ])

    row = bt._simulate_one(
        _entry(stop_loss=9, target=11), bars, holding_period=1,
        include_benchmark=False, as_of_date="2026-08-31",
    )

    assert row["exit_reason"] == "target_hit"
    assert row["exit_price"] == 11
    assert row["net_return"] == pytest.approx(0.1)
    assert row["hit"] is True


def test_flat_non_limit_fill_day_is_not_misclassified_as_unfillable():
    bars = _bars([
        (10, 10, 10, 10),
        (10, 10, 10, 10),
        (10, 10.2, 9.9, 10.1),
    ])

    row = bt._simulate_one(
        _entry(), bars, holding_period=1, include_benchmark=False,
        as_of_date="2026-08-31",
    )

    assert row["status"] == "completed"
    assert row["unfillable"] is False


def test_aggregate_excludes_pending_from_official_statistics(monkeypatch):
    rows = [
        {"status": "completed", "unfillable": False, "hit": True,
         "net_return": 0.1, "confidence": 5, "verdict": "偏多", "strategy": "standalone",
         "exit_reason": "target_hit", "mae": -0.01, "mfe": 0.1, "excess_return": None},
        {"status": "pending", "unfillable": False, "hit": None,
         "net_return": None, "confidence": 5, "verdict": "偏多", "strategy": "standalone",
         "exit_reason": "pending", "mae": -0.02, "mfe": 0.03, "excess_return": None},
    ]
    monkeypatch.setattr(bt, "_prefetch_signals", lambda *args, **kwargs: None)
    monkeypatch.setattr(bt, "_run_entries", lambda *args, **kwargs: (
        rows,
        {"completed": 1, "pending": 1, "data_truncated": 0,
         "unfillable": 0, "no_fill_data": 0},
    ))

    with patch.object(bt.journal, "load_verdicts", return_value=[_entry()]):
        result = bt.aggregate(lookforward_days=10, include_benchmark=False)

    assert result["completed_count"] == 1
    assert result["pending_count"] == 1
    assert result["fillable_count"] == 1
    assert result["by_verdict"] == [{
        "key": "偏多", "n": 1, "win_rate": 1.0,
        "avg_net_return": 0.1, "avg_excess_return": None,
    }]


def test_run_entries_keeps_no_fill_signal_visible(monkeypatch):
    monkeypatch.setattr(bt, "_load_bars_by_code", lambda *args, **kwargs: {})

    rows, counts = bt._run_entries([_entry()], holding_period=10, include_benchmark=False)

    assert len(rows) == 1
    assert rows[0]["status"] == "no_fill_data"
    assert rows[0]["net_return"] is None
    assert rows[0]["hit"] is None
    assert counts["no_fill_data"] == 1


def test_sweep_excludes_pending_from_period_metrics(monkeypatch):
    entries = [_entry(), {**_entry(), "ts_code": "600002.SH"}]
    sample_bars = _bars([(10, 10, 10, 10), (10, 10, 10, 10)])
    monkeypatch.setattr(bt.journal, "load_verdicts", lambda **kwargs: entries)
    monkeypatch.setattr(bt, "_prefetch_signals", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bt, "_load_bars_by_code",
        lambda *args, **kwargs: {entry["ts_code"]: sample_bars for entry in entries},
    )

    def simulate(entry, *args, **kwargs):
        pending = entry["ts_code"] == "600002.SH"
        return {
            "status": "pending" if pending else "completed",
            "unfillable": False,
            "truncated": False,
            "hit": None if pending else True,
            "net_return": None if pending else 0.1,
            "max_drawdown": None,
            "excess_return": None,
        }

    monkeypatch.setattr(bt, "_simulate_one", simulate)

    result = bt.run_sweep(holding_periods=[10], include_benchmark=False)

    period = result["by_period"][0]
    assert period["completed_count"] == 1
    assert period["pending_count"] == 1
    assert period["fillable_n"] == 1
    assert period["win_rate"] == 1.0
    assert period["avg_net_return"] == 0.1


def test_sweep_emits_no_fill_data_rows(monkeypatch):
    monkeypatch.setattr(bt.journal, "load_verdicts", lambda **kwargs: [_entry()])
    monkeypatch.setattr(bt, "_prefetch_signals", lambda *args, **kwargs: None)
    monkeypatch.setattr(bt, "_load_bars_by_code", lambda *args, **kwargs: {})

    result = bt.run_sweep(holding_periods=[10], include_benchmark=False)

    assert result["per_signal"][0]["status"] == "no_fill_data"
    assert result["by_period"][0]["no_fill_bar_count"] == 1


def test_portfolio_signal_builder_leaves_unmatured_position_open():
    index = pd.bdate_range("2026-08-03", periods=4)

    entries, exits, meta = bt._build_signals([_entry()], index, holding_period=10)

    assert entries.values.sum() == 1
    assert exits.values.sum() == 0
    assert meta[0]["status"] == "pending"
    assert meta[0]["exit_date"] is None


def test_portfolio_win_rate_counts_only_closed_trades(monkeypatch):
    index = pd.bdate_range("2026-08-03", periods=4)
    panel = pd.DataFrame({"600001.SH": [10.0, 10.0, 10.5, 11.0]}, index=index)
    panels = {name: panel.copy() for name in ("open", "high", "low", "close")}
    monkeypatch.setattr(bt.journal, "load_verdicts", lambda **kwargs: [_entry()])
    monkeypatch.setattr(bt, "_build_ohlc_panels", lambda *args, **kwargs: panels)

    result = bt.run_portfolio(lookforward_days=10)

    assert result["stats"]["n_trades"] == 0
    assert result["stats"]["open_positions"] == 1
    assert result["stats"]["win_rate"] is None


def test_cli_summary_reports_official_sample_and_pending_separately(capsys):
    df = pd.DataFrame([
        {"status": "completed", "hit": True, "net_return": 0.1,
         "ts_code": "600001.SH", "date": "2026-08-01", "max_drawdown": -0.02},
        {"status": "pending", "hit": None, "net_return": None,
         "ts_code": "600002.SH", "date": "2026-08-20", "max_drawdown": None},
    ])

    bt.print_summary(df)

    output = capsys.readouterr().out
    assert "已完成交易     : 1" in output
    assert "进行中         : 1" in output
    assert "胜率           : 100.0%" in output
