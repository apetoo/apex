"""缠论页后端全量测试（缠论 PR T7）。

覆盖 17 路径：降级 n<30 / 501 / 502 / ETF 路由 / Direction 映射 / confirmed 标记 /
zs_break 四分支 / golden 结构不变量 / BSP 映射 fixture / 机械跳空检测 / is_etf /
normalize 9→BJ / fetch_raw_bars 绕缓存 / BJ 分钟降级 / 周线重采样 / freq 400 / dt 格式。

全部 mock fetch_raw_bars / tushare，无网络。
"""
from datetime import datetime, timedelta

import pytest

from apex import chan, data, technical


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_bars(n=120, start=100.0, wave=8.0, start_dt=datetime(2026, 1, 5)):
    """锯齿波 bars（强制产生笔）：每 10 根反向。"""
    bars, price, direction = [], start, 1
    for i in range(n):
        if i % 10 == 9:
            direction *= -1
        price = price + direction * wave / 10
        o = price - direction * 0.3
        bars.append({
            "dt": start_dt + timedelta(days=i), "open": round(o, 2),
            "high": round(max(o, price) + 0.2, 2), "low": round(min(o, price) - 0.2, 2),
            "close": round(price, 2), "vol": 1000.0, "amount": 100000.0,
        })
    return bars


@pytest.fixture
def mock_bars(monkeypatch):
    bars = _mk_bars()
    monkeypatch.setattr(chan, "fetch_raw_bars", lambda ts_code, n=250, freq="D": bars)
    return bars


# ── T2: is_etf / normalize 9→BJ ─────────────────────────────────────────────

class TestNormalizeAndEtf:
    def test_bj_920_new_segment(self):
        assert data.normalize_ts_code("920185") == "920185.BJ"
        assert data.normalize_ts_code("920810") == "920810.BJ"

    def test_bj_old_codes_still_map(self):
        assert data.normalize_ts_code("838810") == "838810.BJ"
        assert data.normalize_ts_code("430047") == "430047.BJ"

    def test_is_etf_prefix(self):
        assert data.is_etf("510300.SH") and data.is_etf("510300")
        assert data.is_etf("159915.SZ") and data.is_etf("159915")
        assert not data.is_etf("603019.SH")
        assert not data.is_etf("920185.BJ")


# ── T3: fetch_raw_bars 路由（mock tushare/akshare/东财） ────────────────────

class TestFetchRawBarsRouting:
    def test_etf_daily_uses_fund_daily(self, monkeypatch):
        import pandas as pd
        called = {}

        class FakePro:
            def fund_daily(self, **kw):
                called["fund"] = True
                return pd.DataFrame({
                    "ts_code": ["510300.SH"], "trade_date": ["20260807"],
                    "open": [4.7], "high": [4.8], "low": [4.6], "close": [4.75],
                    "vol": [100], "amount": [470],
                })

            def daily(self, **kw):
                called["daily"] = True
                return pd.DataFrame()

        monkeypatch.setattr(data, "_tushare", lambda: FakePro())
        bars = technical.fetch_raw_bars("510300.SH", n=10)
        assert called.get("fund") and not called.get("daily")
        assert bars[-1]["close"] == 4.75

    def test_bypasses_bars_cache(self, monkeypatch):
        """fetch_raw_bars 不得读写 _BARS_CACHE（60/250 根缓存键互污染前科）。"""
        technical._BARS_CACHE["603019.SH"] = [{"close": 999}]
        import pandas as pd
        monkeypatch.setattr(data, "_tushare", lambda: type("P", (), {
            "daily": lambda self, **kw: pd.DataFrame({
                "ts_code": ["603019.SH"], "trade_date": ["20260807"],
                "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.05],
                "vol": [1], "amount": [1]}),
        })())
        bars = technical.fetch_raw_bars("603019.SH", n=10)
        assert bars[0]["close"] == 1.05  # 拿到新数据而非缓存的 999
        assert technical._BARS_CACHE["603019.SH"] == [{"close": 999}]  # 缓存未被写

    def test_bj_minute_raises(self):
        with pytest.raises(technical.DataFetchError, match="BJ 分钟"):
            technical.fetch_raw_bars("920185.BJ", freq="30")

    def test_minute_bars_exclude_current_unfinished_interval(self):
        """东财分钟 K 的 dt 是区间结束时刻；尚未到达的结束时刻不得进入缠论。"""
        bars = [
            {"dt": datetime(2026, 8, 7, 10, 0), "close": 10.0},
            {"dt": datetime(2026, 8, 7, 10, 30), "close": 10.1},
        ]

        completed = technical._completed_minute_bars(
            bars, now=datetime(2026, 8, 7, 10, 15)
        )

        assert [b["dt"] for b in completed] == [datetime(2026, 8, 7, 10, 0)]

    def test_minute_bars_keep_interval_at_its_close_time(self):
        bars = [{"dt": datetime(2026, 8, 7, 10, 30), "close": 10.1}]

        completed = technical._completed_minute_bars(
            bars, now=datetime(2026, 8, 7, 10, 30)
        )

        assert completed == bars

    def test_all_sources_fail_raises_502_error(self, monkeypatch):
        monkeypatch.setattr(data, "_tushare", lambda: type("P", (), {
            "daily": lambda self, **kw: None})())
        monkeypatch.setattr(technical, "_akshare_daily_bars", lambda c, n: None)
        with pytest.raises(technical.DataFetchError):
            technical.fetch_raw_bars("603019.SH", n=10)

    def test_weekly_resample_etf(self):
        bars = _mk_bars(n=70)
        out = technical._resample_weekly(bars, 10)
        assert 0 < len(out) <= 10
        for w in out:
            assert w["low"] <= w["open"] <= w["high"] or w["low"] <= w["close"] <= w["high"]
            assert w["high"] >= w["low"]


# ── T4: 结构计算（mock fetch_raw_bars） ──────────────────────────────────────

class TestStructure:
    def test_degrade_insufficient_bars(self, monkeypatch):
        monkeypatch.setattr(chan, "fetch_raw_bars",
                            lambda ts_code, n=250, freq="D": _mk_bars(n=20))
        s = chan.get_structure("603019.SH")
        assert s["summary"]["reason"] == "insufficient_bars"
        assert s["bi_list"] == [] and len(s["bars"]) == 20  # K 线照画
        assert s["decision"]["state"] == "watching"
        assert s["decision"]["candidate_eligible"] is False

    def test_direction_mapping_and_confirmed(self, mock_bars):
        s = chan.get_structure("603019.SH")
        assert s["bi_list"], "锯齿数据必须产生笔"
        dirs = {b["direction"] for b in s["bi_list"]}
        assert dirs <= {"up", "down"}  # Direction 映射无泄漏（不出现 Direction.Up repr）
        assert all(isinstance(b["confirmed"], bool) for b in s["bi_list"])

    def test_bi_direction_alternates(self, mock_bars):
        """golden 不变量：笔方向交替。"""
        s = chan.get_structure("603019.SH")
        dirs = [b["direction"] for b in s["bi_list"]]
        for a, b in zip(dirs, dirs[1:]):
            assert a != b

    def test_zs_within_price_range(self, mock_bars):
        """golden 不变量：中枢区间在价格范围内、zg>=zd。"""
        s = chan.get_structure("603019.SH")
        lo = min(b["low"] for b in s["bars"])
        hi = max(b["high"] for b in s["bars"])
        for z in s["zs_list"]:
            assert z["zg"] >= z["zd"]
            assert lo - 1 <= z["zd"] and z["zg"] <= hi + 1
            assert z["state"] in ("confirmed", "extending")

    def test_zs_last_is_extending(self, mock_bars):
        """confirmed-zs 启发式：最后一条恒 extending，之前恒 confirmed。"""
        s = chan.get_structure("603019.SH")
        if len(s["zs_list"]) > 1:
            assert s["zs_list"][-1]["state"] == "extending"
            assert all(z["state"] == "confirmed" for z in s["zs_list"][:-1])

    def test_dt_format_daily_is_date_only(self, mock_bars):
        s = chan.get_structure("603019.SH", freq="D")
        assert len(s["bars"][0]["dt"]) == 10  # YYYY-MM-DD
        assert all(len(b["sdt"]) == 10 for b in s["bi_list"])

    def test_bad_freq_raises(self, mock_bars):
        with pytest.raises(ValueError):
            chan.get_structure("603019.SH", freq="X")


class TestZsBreak:
    """四分支：up / down / inside / none（含跳空过滤）。"""

    def _zs(self, zg=110.0, zd=90.0, sdt="2026-01-10", state="confirmed"):
        return {"sdt": sdt, "edt": "2026-03-01", "zg": zg, "zd": zd,
                "zz": (zg + zd) / 2, "state": state}

    def test_up(self):
        zb, z = chan._zs_break(115.0, [self._zs()], None, [], "D")
        assert zb == "up" and z is not None

    def test_down(self):
        zb, _ = chan._zs_break(85.0, [self._zs()], None, [], "D")
        assert zb == "down"

    def test_inside(self):
        zb, _ = chan._zs_break(100.0, [self._zs()], None, [], "D")
        assert zb == "inside"

    def test_none_when_no_confirmed(self):
        zb, z = chan._zs_break(100.0, [self._zs(state="extending")], None, [], "D")
        assert zb == "none" and z is None

    def test_none_when_zs_predates_gap(self):
        """除权跳空后的 zs_break：跳空前形成的中枢不可用。"""
        bars = [{"dt": datetime(2026, 6, 1)}]
        zb, z = chan._zs_break(100.0, [self._zs(sdt="2026-01-10")], 0, bars, "D")
        assert zb == "none" and z is None
        zb2, z2 = chan._zs_break(115.0, [self._zs(sdt="2026-06-05")], 0, bars, "D")
        assert zb2 == "up" and z2 is not None  # 跳空后形成的中枢可用


class TestExDivGap:
    def test_detects_mechanical_gap(self):
        bars = _mk_bars(n=40)
        # 人工造 10送4 除权跳空：第 30 根 open 较昨收 -28.6%，前日非涨跌停
        bars[30]["open"] = round(bars[29]["close"] / 1.4, 2)
        bars[30]["low"] = min(bars[30]["low"], bars[30]["open"])
        idx = chan._detect_last_ex_div_gap(bars, "603019.SH")
        assert idx == 30

    def test_ignores_normal_moves(self):
        assert chan._detect_last_ex_div_gap(_mk_bars(n=40), "603019.SH") is None

    def test_ignores_gap_after_limit_day(self):
        """涨跌停日后的跳空是真实走势，不算机械跳空。"""
        bars = _mk_bars(n=40)
        bars[28]["close"] = 100.0
        bars[29]["open"] = 100.0
        bars[29]["close"] = 110.0  # 前收→收盘 +10%，真涨停
        bars[29]["low"] = min(bars[29]["low"], bars[29]["open"])
        bars[30]["open"] = round(bars[29]["close"] * 1.12, 2)  # 次日再跳空 12%
        assert chan._detect_last_ex_div_gap(bars, "603019.SH") is None

    def test_large_intraday_body_is_not_a_limit_day(self):
        """防回归：前日实体 10% 但相对前收未涨停，不得屏蔽除权跳空。"""
        bars = _mk_bars(n=40)
        bars[28]["close"] = 100.0
        bars[29]["open"] = 90.0
        bars[29]["close"] = 99.0  # 实体 +10%，但相对前收仅 -1%
        bars[30]["open"] = 70.0   # 相对昨收 -29.3%

        assert chan._detect_last_ex_div_gap(bars, "603019.SH") == 30


class TestBspMapping:
    """BSP 映射 fixture：mock 信号函数返回固定值，验证映射链。"""

    def test_signal_value_to_type(self, monkeypatch):
        monkeypatch.setattr(chan, "cxt_first_buy_V221126",
                            lambda c, di=1: {"日线_D1B_BUY1": "一买_5笔_任意_0"})
        monkeypatch.setattr(chan, "cxt_first_sell_V221126", lambda c, di=1: {"k": "其他_任意_任意_0"})
        monkeypatch.setattr(chan, "cxt_second_bs_V240524", lambda c, di=1: {"k": "其他_任意_任意_0"})
        monkeypatch.setattr(chan, "cxt_third_bs_V230319", lambda c, di=1: {"k": "其他_任意_任意_0"})
        assert chan._eval_bsp(object()) == "一买"
        assert chan._BSP_TYPE_MAP["一买"] == "1buy"

    def test_no_match_returns_none(self, monkeypatch):
        for name in ("cxt_first_buy_V221126", "cxt_first_sell_V221126",
                     "cxt_second_bs_V240524", "cxt_third_bs_V230319"):
            monkeypatch.setattr(chan, name, lambda c, di=1: {"k": "其他_任意_任意_0"})
        assert chan._eval_bsp(object()) is None

    def test_signal_exception_isolated(self, monkeypatch):
        """单个信号函数抛异常不影响其它信号评估。"""
        def boom(c, di=1):
            raise RuntimeError("signal bug")
        monkeypatch.setattr(chan, "cxt_first_buy_V221126", boom)
        monkeypatch.setattr(chan, "cxt_first_sell_V221126", lambda c, di=1: {"k": "一卖_任意_任意_0"})
        monkeypatch.setattr(chan, "cxt_second_bs_V240524", lambda c, di=1: {"k": "其他_任意_任意_0"})
        monkeypatch.setattr(chan, "cxt_third_bs_V230319", lambda c, di=1: {"k": "其他_任意_任意_0"})
        assert chan._eval_bsp(object()) == "一卖"

    def test_bsp_entries_have_approximate_flag(self, mock_bars):
        s = chan.get_structure("603019.SH")
        for b in s["bsp_list"]:
            assert b["approximate"] is True
            assert b["type"] in ("1buy", "1sell", "2buy", "2sell", "3buy", "3sell")


class TestChanDecision:
    EMPTY_DECISION = {
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

    @staticmethod
    def _bars(closes=(10.2,)):
        start = datetime(2026, 1, 5)
        return [{
            "dt": start + timedelta(days=i), "open": close,
            "high": 10.5 if i == 0 else close + 0.1,
            "low": close - 0.1, "close": close, "vol": 1000,
        } for i, close in enumerate(closes)]

    @staticmethod
    def _bsp():
        return {"dt": "2026-01-05", "type": "2buy", "price": 10.0}

    def test_empty_decision_has_stable_shape(self):
        assert chan._build_decision(self._bars(), [], "none", None, "D") == self.EMPTY_DECISION

    def test_buy_waits_for_signal_bar_high_confirmation(self):
        decision = chan._build_decision(self._bars(), [self._bsp()], "none", None, "D")
        assert decision["confirm_price"] == 10.5
        assert decision["invalidation_price"] == 10.0
        assert decision["state"] == "pending"
        assert decision["candidate_eligible"] is True

    def test_buy_is_confirmed_after_later_close_above_confirmation(self):
        decision = chan._build_decision(self._bars((10.2, 10.6)), [self._bsp()], "none", None, "D")
        assert decision["state"] == "confirmed"
        assert decision["candidate_eligible"] is True

    def test_current_close_below_invalidation_wins_over_confirmation(self):
        decision = chan._build_decision(self._bars((10.2, 10.6, 9.9)), [self._bsp()], "none", None, "D")
        assert decision["state"] == "invalid"
        assert decision["candidate_eligible"] is False

    def test_buy_older_than_ten_bars_is_stale(self):
        decision = chan._build_decision(self._bars((10.2,) * 12), [self._bsp()], "none", None, "D")
        assert decision["bars_since_signal"] == 11
        assert decision["ineligible_reason"] == "stale_signal"
        assert decision["candidate_eligible"] is False

    def test_upward_breakout_has_retest_band(self):
        decision = chan._build_decision(
            self._bars(), [], "up", {"zg": 12, "zd": 10}, "D"
        )
        assert decision["setup"] == "zs_breakout"
        assert decision["state"] == "confirmed"
        assert decision["trigger_price"] == 12
        assert decision["trigger_low"] == 12
        assert decision["trigger_high"] == 12.12
        assert decision["invalidation_price"] == 10
        assert decision["candidate_eligible"] is True

    def test_downward_break_is_risk_and_ineligible(self):
        decision = chan._build_decision(
            self._bars(), [], "down", {"zg": 12, "zd": 10}, "D"
        )
        assert decision["bias"] == "risk"
        assert decision["state"] == "invalid"
        assert decision["candidate_eligible"] is False

    def test_recent_sell_is_risk_without_valid_recent_buy(self):
        sell = {"dt": "2026-01-05", "type": "2sell", "price": 10.0}
        decision = chan._build_decision(self._bars(), [sell], "none", None, "D")
        assert decision["bias"] == "risk"
        assert decision["candidate_eligible"] is False

    def test_recent_valid_buy_outranks_upward_breakout(self):
        decision = chan._build_decision(
            self._bars(), [self._bsp()], "up", {"zg": 12, "zd": 10}, "D"
        )
        assert decision["setup"] == "bsp_buy"

    def test_expired_buy_falls_through_to_breakout(self):
        decision = chan._build_decision(
            self._bars((10.2,) * 12), [self._bsp()], "up", {"zg": 12, "zd": 10}, "D"
        )
        assert decision["setup"] == "zs_breakout"
        assert decision["candidate_eligible"] is True

    def test_expired_buy_remains_visible_without_breakout(self):
        decision = chan._build_decision(self._bars((10.2,) * 12), [self._bsp()], "none", None, "D")
        assert decision["setup"] == "bsp_buy"
        assert decision["state"] == "pending"
        assert decision["candidate_eligible"] is False
        assert decision["ineligible_reason"] == "stale_signal"

    def test_missing_pivot_data_never_creates_candidate_prices(self):
        decision = chan._build_decision(self._bars(), [], "up", None, "D")
        assert decision["candidate_eligible"] is False
        assert decision["trigger_price"] is None
        assert decision["invalidation_price"] is None


# ── T5: HTTP 层 ─────────────────────────────────────────────────────────────

class TestHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from backend.main import app
        return TestClient(app, raise_server_exceptions=False)

    def test_200_shape(self, client, mock_bars):
        r = client.get("/api/chan/603019.SH")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"ts_code", "freq", "bars", "bi_list",
                             "zs_list", "bsp_list", "summary", "decision"}

    def test_400_bad_freq(self, client, mock_bars):
        r = client.get("/api/chan/603019.SH", params={"freq": "X"})
        assert r.status_code == 400

    def test_502_on_data_failure(self, client, monkeypatch):
        def boom(ts_code, n=250, freq="D"):
            raise technical.DataFetchError("tushare empty; akshare empty")
        monkeypatch.setattr(chan, "fetch_raw_bars", boom)
        r = client.get("/api/chan/603019.SH")
        assert r.status_code == 502
        assert "数据获取失败" in r.json()["detail"]

    def test_501_when_czsc_missing(self, client, monkeypatch):
        monkeypatch.setattr(chan, "_CZSC_AVAILABLE", False)
        r = client.get("/api/chan/603019.SH")
        assert r.status_code == 501

    def test_bj_minute_degrade_message(self, client):
        r = client.get("/api/chan/920810.BJ", params={"freq": "30"})
        assert r.status_code == 502
        assert "BJ 分钟" in r.json()["detail"]


# ── 补丁：非法代码 400（QA Edge Case） ───────────────────────────────────────

class TestInputValidation:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from backend.main import app
        return TestClient(app, raise_server_exceptions=False)

    def test_invalid_code_returns_400(self, client):
        """非法代码（非 6 位数字）-> 400，不是 502（QA 边界）。"""
        for bad in ["abc", "12345", "6030190"]:  # 字母 / 5 位 / 7 位
            r = client.get(f"/api/chan/{bad}")
            assert r.status_code == 400, f"{bad}: {r.status_code} {r.text}"

    def test_bare_6digit_code_normalizes_and_passes(self, client, mock_bars):
        """裸 6 位代码是正常输入（normalize 补后缀），不应 400。"""
        for good in ["603019", "920810", "510300"]:
            r = client.get(f"/api/chan/{good}")
            assert r.status_code == 200, f"{good}: {r.status_code}"

    def test_valid_codes_pass(self, client, mock_bars):
        for good in ["603019.SH", "920810.BJ", "510300.SH", "920185"]:
            r = client.get(f"/api/chan/{good}")
            assert r.status_code == 200, f"{good}: {r.status_code}"
