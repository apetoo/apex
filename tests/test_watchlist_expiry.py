"""候选动态过期天数（波动率驱动）单测。

覆盖 realized_vol 纯函数 + _dynamic_expiry_days 业务函数 + add_candidate 端到端。
mock get_daily_price 喂构造日线, 不触 tushare/akshare 网络。
"""
import json
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from apex import technical
from apex import watchlist as wl


# ── helpers ───────────────────────────────────────────────────────────────────


def _bars(closes):
    """close 序列 → get_daily_price 解析后的 list[dict]（升序, 含 close/trade_date）。"""
    return [
        {"trade_date": f"202601{i:02d}", "close": c, "open": c, "high": c, "low": c, "vol": 100}
        for i, c in enumerate(closes)
    ]


def _zigzag(n: int, amp: float):
    """n+1 根交替 ±amp 收益率的日线（升序）。amp=0.05 ≈ ±5% 日波动。"""
    closes = [100.0]
    for i in range(n):
        closes.append(closes[-1] * (1 + amp if i % 2 == 0 else 1 / (1 + amp)))
    return _bars(closes)


# ── realized_vol ──────────────────────────────────────────────────────────────


def test_realized_vol_insufficient_bars():
    assert technical.realized_vol(_bars([100.0] * 10), window=20) is None


def test_realized_vol_constant_series_returns_none():
    # 全平 → pct_change 全 0 → std=0 → None
    assert technical.realized_vol(_bars([100.0] * 25), window=20) is None


def test_realized_vol_known_value():
    closes = [100.0]
    for i in range(24):
        closes.append(closes[-1] * (1.05 if i % 2 == 0 else 1 / 1.05))
    sigma = technical.realized_vol(_bars(closes), window=20)
    assert sigma is not None
    expected = float(pd.Series(closes[-21:]).pct_change().dropna().std())
    assert sigma == round(expected, 6)


# ── _dynamic_expiry_days ──────────────────────────────────────────────────────


@patch("apex.data.get_daily_price")
def test_dynamic_expiry_short_when_close_and_volatile(mock_get):
    # distance≈1%, 高波动 σ≈5% → t* 极小 → 夹到 floor 3
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    days, meta = wl._dynamic_expiry_days("601012.SH", trigger_price=101.0)
    assert days == 3
    assert meta["method"] == "vol_based"
    assert meta["realized_vol"] > 0
    assert meta["distance_pct"] > 0


@patch("apex.data.get_daily_price")
def test_dynamic_expiry_long_when_far_and_quiet(mock_get):
    # distance≈23%, 低波动 σ≈0.5% → t* 巨大 → 夹到 ceiling 30
    mock_get.return_value = json.dumps(_zigzag(24, 0.005))
    days, meta = wl._dynamic_expiry_days("601012.SH", trigger_price=130.0)
    assert days == 30
    assert meta["method"] == "vol_based"


@patch("apex.data.get_daily_price")
def test_dynamic_expiry_fallback_on_api_error(mock_get):
    mock_get.side_effect = Exception("tushare down")
    days, meta = wl._dynamic_expiry_days("601012.SH", trigger_price=22.5)
    assert days == 7
    assert meta["method"] == "fallback"


@patch("apex.data.get_daily_price")
def test_dynamic_expiry_fallback_on_empty_bars(mock_get):
    mock_get.return_value = json.dumps([])
    days, meta = wl._dynamic_expiry_days("601012.SH", trigger_price=22.5)
    assert days == 7
    assert meta["method"] == "fallback"


# ── add_candidate 端到端 ──────────────────────────────────────────────────────


@pytest.fixture
def wl_file(tmp_path, monkeypatch):
    f = tmp_path / "wl.json"
    monkeypatch.setattr("apex.config.get", lambda: {"paths": {"watchlist_file": str(f)}})
    return f


@patch("apex.data.get_daily_price")
def test_add_candidate_dynamic_when_none(mock_get, wl_file):
    # expires_days 默认 None → 动态算
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    meta = wl.add_candidate("601012.SH", "隆基绿能", trigger_price=101.0)
    assert meta["method"] == "vol_based"
    assert meta["expires_days"] == 3
    saved = json.loads(wl_file.read_text(encoding="utf-8"))
    cand = saved["candidates"][-1]
    assert cand["ts_code"] == "601012.SH"
    assert cand["expires_at"] == (date.today() + timedelta(days=meta["expires_days"])).isoformat()


@patch("apex.data.get_daily_price")
def test_add_candidate_manual_override(mock_get, wl_file):
    # 显式传 expires_days → 手动覆盖, 不调 get_daily_price
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    meta = wl.add_candidate("601012.SH", "隆基绿能", trigger_price=101.0, expires_days=14)
    assert meta["method"] == "manual"
    assert meta["expires_days"] == 14
    mock_get.assert_not_called()
    saved = json.loads(wl_file.read_text(encoding="utf-8"))
    cand = saved["candidates"][-1]
    assert cand["expires_at"] == (date.today() + timedelta(days=14)).isoformat()


@patch("apex.data.get_daily_price")
def test_add_candidate_fallback_when_data_missing(mock_get, wl_file):
    mock_get.side_effect = Exception("down")
    meta = wl.add_candidate("601012.SH", "隆基绿能", trigger_price=22.5)  # None → 动态 → fallback
    assert meta["method"] == "fallback"
    assert meta["expires_days"] == 7


# ── expires_meta 持久化 ───────────────────────────────────────────────────────


@patch("apex.data.get_daily_price")
def test_add_candidate_writes_expires_meta_vol_based(mock_get, wl_file):
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    wl.add_candidate("601012.SH", "隆基绿能", trigger_price=101.0)
    saved = json.loads(wl_file.read_text(encoding="utf-8"))
    meta = saved["candidates"][-1]["expires_meta"]
    assert meta["method"] == "vol_based"
    assert meta["expires_days"] == 3
    assert meta["realized_vol"] > 0
    assert meta["distance_pct"] > 0


@patch("apex.data.get_daily_price")
def test_add_candidate_writes_expires_meta_manual(mock_get, wl_file):
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    wl.add_candidate("601012.SH", "隆基绿能", trigger_price=101.0, expires_days=14)
    saved = json.loads(wl_file.read_text(encoding="utf-8"))
    meta = saved["candidates"][-1]["expires_meta"]
    assert meta == {"expires_days": 14, "method": "manual"}


@patch("apex.data.get_daily_price")
def test_add_candidate_writes_expires_meta_fallback(mock_get, wl_file):
    mock_get.side_effect = Exception("down")
    wl.add_candidate("601012.SH", "隆基绿能", trigger_price=22.5)
    saved = json.loads(wl_file.read_text(encoding="utf-8"))
    meta = saved["candidates"][-1]["expires_meta"]
    assert meta == {"expires_days": 7, "method": "fallback"}


# ── renew_candidate ───────────────────────────────────────────────────────────


@patch("apex.data.get_daily_price")
def test_renew_candidate_recomputes_and_increments_count(mock_get, wl_file):
    # 先加一个候选(过期态: 用旧 expires_days 手动设短)
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    wl.add_candidate("601012.SH", "隆基绿能", trigger_price=101.0, expires_days=1)
    # 续期: 用今天波动率重算
    result = wl.renew_candidate("601012.SH")
    assert result is not None
    assert result["method"] == "vol_based"
    assert result["renew_count"] == 1
    saved = json.loads(wl_file.read_text(encoding="utf-8"))
    cand = saved["candidates"][-1]
    assert cand["renew_count"] == 1
    assert cand["expires_meta"]["method"] == "vol_based"
    # expires_at 应是今天 + resolved 天
    assert cand["expires_at"] == (date.today() + timedelta(days=result["expires_days"])).isoformat()


@patch("apex.data.get_daily_price")
def test_renew_candidate_multiple_times(mock_get, wl_file):
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    wl.add_candidate("601012.SH", "隆基绿能", trigger_price=101.0, expires_days=1)
    wl.renew_candidate("601012.SH")
    result2 = wl.renew_candidate("601012.SH")
    assert result2["renew_count"] == 2
    saved = json.loads(wl_file.read_text(encoding="utf-8"))
    assert saved["candidates"][-1]["renew_count"] == 2


@patch("apex.data.get_daily_price")
def test_renew_candidate_returns_none_when_not_found(mock_get, wl_file):
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    assert wl.renew_candidate("999999.SZ") is None


@patch("apex.data.get_daily_price")
def test_renew_candidate_fallback_when_data_missing(mock_get, wl_file):
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    wl.add_candidate("601012.SH", "隆基绿能", trigger_price=101.0, expires_days=1)
    mock_get.side_effect = Exception("down")  # 续期时数据缺失
    result = wl.renew_candidate("601012.SH")
    assert result["method"] == "fallback"
    assert result["expires_days"] == 7
    assert result["renew_count"] == 1


# ── sync_candidate_from_journal ───────────────────────────────────────────────


def _journal_entry(entry, stop, target, analyzed_at="2026-07-01T09:53:52+08:00"):
    return {
        "ts_code": "601012.SH",
        "analyzed_at": analyzed_at,
        "verdict": "偏多",
        "confidence": 4,
        "price_advice": {"entry": entry, "stop_loss": stop, "target": target, "position_size_pct": 10},
    }


@patch("apex.data.get_daily_price")
@patch("apex.journal.load_latest")
def test_sync_candidate_overwrites_three_fields(mock_journal, mock_get, wl_file):
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    # 先加候选 trigger=101, stop=21, target=25(老值)
    wl.add_candidate("601012.SH", "隆基绿能", trigger_price=101.0,
                     stop_advice=21.0, target_advice=25.0, expires_days=1)
    # journal 最近分析: entry=68.7 stop=65.5 target=76(新值)
    mock_journal.return_value = _journal_entry(68.7, 65.5, 76)
    result = wl.sync_candidate_from_journal("601012.SH")
    assert result is not None
    assert result["trigger_price"] == 68.7
    assert result["stop_advice"] == 65.5
    assert result["target_advice"] == 76
    assert result["analyzed_at"] == "2026-07-01T09:53:52+08:00"
    saved = json.loads(wl_file.read_text(encoding="utf-8"))
    cand = saved["candidates"][-1]
    assert cand["trigger_price"] == 68.7
    assert cand["stop_advice"] == 65.5
    assert cand["target_advice"] == 76
    # trigger 变了 → expires_at 也重算(有 expires_meta)
    assert cand["expires_meta"]["method"] in ("vol_based", "fallback")


@patch("apex.data.get_daily_price")
@patch("apex.journal.load_latest")
def test_sync_candidate_returns_none_when_no_journal(mock_journal, mock_get, wl_file):
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    wl.add_candidate("601012.SH", "隆基绿能", trigger_price=101.0)
    mock_journal.return_value = None  # 无分析记录
    assert wl.sync_candidate_from_journal("601012.SH") is None


@patch("apex.data.get_daily_price")
@patch("apex.journal.load_latest")
def test_sync_candidate_returns_none_when_entry_missing(mock_journal, mock_get, wl_file):
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    wl.add_candidate("601012.SH", "隆基绿能", trigger_price=101.0)
    mock_journal.return_value = _journal_entry(None, 65.5, 76)  # entry 缺
    assert wl.sync_candidate_from_journal("601012.SH") is None


@patch("apex.data.get_daily_price")
@patch("apex.journal.load_latest")
def test_sync_candidate_returns_none_when_candidate_not_found(mock_journal, mock_get, wl_file):
    mock_get.return_value = json.dumps(_zigzag(24, 0.05))
    mock_journal.return_value = _journal_entry(68.7, 65.5, 76)
    assert wl.sync_candidate_from_journal("999999.SZ") is None
