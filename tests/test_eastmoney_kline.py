"""东财 K 线传输边界测试：直连、重试、解析与缓存。"""
from datetime import datetime
import logging

import pytest
import requests

from apex import data


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self.payload


@pytest.fixture(autouse=True)
def clear_eastmoney_state():
    cache = getattr(data, "_EM_KLINE_CACHE", None)
    if cache is not None:
        cache.clear()


def test_eastmoney_session_ignores_proxy_environment():
    assert data._eastmoney_session().trust_env is False


def test_eastmoney_kline_uses_current_params_and_parses_bar(monkeypatch):
    calls = []
    payload = {"data": {"klines": [
        "2026-08-07 15:00,25.4,25.78,25.95,25.40,484803.85,12400000"
    ]}}

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(payload)

    monkeypatch.setattr(data, "_eastmoney_session", lambda: Session())
    bars = data.eastmoney_kline("002648.SZ", freq="60", n=250)

    assert bars == [{
        "dt": datetime(2026, 8, 7, 15, 0),
        "open": 25.4,
        "close": 25.78,
        "high": 25.95,
        "low": 25.4,
        "vol": 484803.85,
        "amount": 12400000.0,
    }]
    params = calls[0][1]["params"]
    assert params["ut"] == "7eea3edcaed734bea9cbfc24409ed989"
    assert params["klt"] == 60
    assert params["lmt"] == 250


def test_retries_one_connection_error_then_succeeds(monkeypatch):
    outcomes = [
        requests.ConnectionError("closed"),
        FakeResponse({"data": {"klines": [
            "2026-08-07 15:00,1,2,3,0.5,10,20"
        ]}}),
    ]

    class Session:
        def get(self, *args, **kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(data, "_eastmoney_session", lambda: Session())
    assert data.eastmoney_kline("002648.SZ", "60") is not None
    assert outcomes == []


def test_two_connection_errors_return_none(monkeypatch):
    calls = 0

    class Session:
        def get(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            raise requests.ConnectionError("closed")

    monkeypatch.setattr(data, "_eastmoney_session", lambda: Session())
    assert data.eastmoney_kline("002648.SZ", "60") is None
    assert calls == 2


@pytest.mark.parametrize("response", [
    FakeResponse({}, status_code=400),
    FakeResponse({"data": {"klines": []}}),
])
def test_non_transient_failure_is_not_retried(monkeypatch, response):
    calls = 0

    class Session:
        def get(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return response

    monkeypatch.setattr(data, "_eastmoney_session", lambda: Session())
    assert data.eastmoney_kline("002648.SZ", "60") is None
    assert calls == 1


def test_invalid_json_is_not_retried(monkeypatch):
    calls = 0

    class InvalidJsonResponse(FakeResponse):
        def json(self):
            raise requests.JSONDecodeError("bad json", "x", 0)

    class Session:
        def get(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return InvalidJsonResponse(None)

    monkeypatch.setattr(data, "_eastmoney_session", lambda: Session())
    assert data.eastmoney_kline("002648.SZ", "60") is None
    assert calls == 1


def test_success_cache_key_contains_code_freq_and_n(monkeypatch):
    calls = []

    class Session:
        def get(self, url, **kwargs):
            calls.append(kwargs["params"].copy())
            return FakeResponse({"data": {"klines": [
                "2026-08-07 15:00,1,2,3,0.5,10,20"
            ]}})

    monkeypatch.setattr(data, "_eastmoney_session", lambda: Session())
    data.eastmoney_kline("002648.SZ", "60", 250)
    data.eastmoney_kline("002648.SZ", "60", 250)
    data.eastmoney_kline("002648.SZ", "30", 250)
    data.eastmoney_kline("002648.SZ", "60", 120)
    data.eastmoney_kline("002649.SZ", "60", 250)
    assert len(calls) == 4


def test_expired_success_cache_is_refetched(monkeypatch):
    calls = 0

    class Session:
        def get(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return FakeResponse({"data": {"klines": [
                "2026-08-07 15:00,1,2,3,0.5,10,20"
            ]}})

    monotonic_values = iter([100.0, 161.0])
    monkeypatch.setattr(data, "_eastmoney_session", lambda: Session())
    monkeypatch.setattr(data.time, "monotonic", lambda: next(monotonic_values))
    data.eastmoney_kline("002648.SZ", "60", 250)
    data.eastmoney_kline("002648.SZ", "60", 250)
    assert calls == 2


def test_failure_is_not_cached(monkeypatch):
    calls = 0

    class Session:
        def get(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            raise requests.ConnectionError("closed")

    monkeypatch.setattr(data, "_eastmoney_session", lambda: Session())
    assert data.eastmoney_kline("002648.SZ", "60") is None
    assert data.eastmoney_kline("002648.SZ", "60") is None
    assert calls == 4


def test_daily_fallback_is_not_cached(monkeypatch):
    calls = 0

    class Session:
        def get(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return FakeResponse({"data": {"klines": [
                "2026-08-07,1,2,3,0.5,10,20"
            ]}})

    monkeypatch.setattr(data, "_eastmoney_session", lambda: Session())
    assert data.eastmoney_kline("002648.SZ", "D") is not None
    assert data.eastmoney_kline("002648.SZ", "D") is not None
    assert calls == 2


def test_sina_minute_kline_maps_symbol_and_parses_bars(monkeypatch):
    calls = []
    payload = [{
        "day": "2026-08-10 13:30:00",
        "open": "86.360",
        "high": "86.390",
        "low": "86.000",
        "close": "86.190",
        "volume": "2481595",
        "amount": "213820873.8099",
    }]

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(payload)

    monkeypatch.setattr(data, "_sina_session", lambda: Session())
    bars = data.sina_minute_kline("603019.SH", freq="30", n=250)

    assert bars == [{
        "dt": datetime(2026, 8, 10, 13, 30),
        "open": 86.36,
        "close": 86.19,
        "high": 86.39,
        "low": 86.0,
        "vol": 2481595.0,
        "amount": 213820873.8099,
    }]
    assert calls[0][1]["params"] == {
        "symbol": "sh603019", "scale": 30, "ma": "no", "datalen": 250,
    }


def test_minute_route_falls_back_to_sina_after_eastmoney_failure(monkeypatch):
    bars = [{
        "dt": datetime(2026, 8, 10, 11, 30),
        "open": 1.0, "close": 2.0, "high": 3.0, "low": 0.5,
        "vol": 10.0, "amount": 20.0,
    }]
    calls = []
    monkeypatch.setattr(data, "eastmoney_kline", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        data, "sina_minute_kline",
        lambda ts_code, freq, n: calls.append((ts_code, freq, n)) or bars,
    )

    assert data.minute_kline("603019.SH", "30", 250) == bars
    assert calls == [("603019.SH", "30", 250)]


def test_minute_route_does_not_call_sina_when_eastmoney_succeeds(monkeypatch):
    bars = [{"dt": datetime(2026, 8, 10, 11, 30), "close": 2.0}]
    monkeypatch.setattr(data, "eastmoney_kline", lambda *args, **kwargs: bars)

    def unexpected_sina(*args, **kwargs):
        raise AssertionError("新浪不应在东财成功时调用")

    monkeypatch.setattr(data, "sina_minute_kline", unexpected_sina, raising=False)
    assert data.minute_kline("002648.SZ", "60", 250) == bars


def test_successful_sina_fallback_does_not_emit_warning(monkeypatch, caplog):
    bars = [{"dt": datetime(2026, 8, 10, 11, 30), "close": 2.0}]
    monkeypatch.setattr(data, "eastmoney_kline", lambda *args, **kwargs: None)
    monkeypatch.setattr(data, "sina_minute_kline", lambda *args, **kwargs: bars)

    with caplog.at_level(logging.WARNING, logger="apex.data"):
        assert data.minute_kline("603019.SH", "30", 250) == bars
    assert caplog.records == []


def test_all_minute_sources_failed_emits_warning(monkeypatch, caplog):
    monkeypatch.setattr(data, "eastmoney_kline", lambda *args, **kwargs: None)
    monkeypatch.setattr(data, "sina_minute_kline", lambda *args, **kwargs: None)

    with caplog.at_level(logging.WARNING, logger="apex.data"):
        assert data.minute_kline("603019.SH", "30", 250) is None
    assert "all minute kline sources failed" in caplog.text
