"""加减仓 adherence 事后比对测试（design: wanmingyu-develop-design-20260727-001422）。

覆盖：
- premise 1 矩阵全行（add/trim/exit × buy/sell × 清仓/部分减仓）
- D6 scale_plan：hold 带 ladder 时按 fill_price 比对 add/trim trigger（命中/未命中/空）
- D7 cycle 过滤：entry_date <= analyzed_at <= when，排除上周期 position_action + 取窗口内最近
- stale 边界：14 天整 fresh / 15 天 stale
- D3 best-effort：journal IO 异常 -> None；malformed scale_plan -> na_no_trigger
- T4 聚合：_position_action_adherence 全桶 + stale + malformed
- T2/T3 集成：buy/sell 经 append_trade 写入 trade.adherence

tempdir 隔离：monkeypatch apex.config.get 让 journal_dir / watchlist_file 指向 tmp_path。
"""
import pytest

from apex import journal, trades, watchlist
from apex.trades import _adherence_for
from apex.system import _position_action_adherence

_WHEN = "2026-07-27T14:00:00+08:00"
_ENTRY = "2026-07-10"
_TS = "603019.SH"

_SCALE = [
    {"level": 1, "action": "add", "trigger_price": 96.8, "shares": 200},
    {"level": 2, "action": "trim", "trigger_price": 87.0, "shares": 200},
]


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    fake_cfg = {"paths": {"journal_dir": str(tmp_path),
                          "watchlist_file": str(tmp_path / "watchlist.json")}}
    monkeypatch.setattr("apex.config.get", lambda: fake_cfg)
    return tmp_path


def _write_pa(action="hold", scale_plan=None,
              analyzed_at="2026-07-20T10:00:00+08:00", ts_code=_TS, name="test"):
    """造一条 position_action journal entry 到 tmp_path。"""
    journal.write_entry({
        "ts_code": ts_code, "name": name,
        "date": analyzed_at[:10], "analyzed_at": analyzed_at,
        "source": "position_action",
        "position_action": {"action": action, "scale_plan": scale_plan or [],
                            "add_shares": None, "trim_shares": None,
                            "trim_pct": None, "new_stop": None, "rationale": "test"},
        "verdict": None, "confidence": None, "price_advice": None,
        "features": None, "evidence": None, "analysis_text": "",
    })


def _adh(side, fill_price, shares_after=0, entry_date=_ENTRY, when=_WHEN, ts_code=_TS):
    return _adherence_for(ts_code=ts_code, side=side, fill_price=fill_price,
                          when=when, shares_after=shares_after, entry_date=entry_date)


# ── premise 1 矩阵全行（headline action）──────────────────────────────────────

@pytest.mark.parametrize("action,side,shares_after,exp_followed,exp_partial", [
    ("add",  "buy",  100, True,  False),   # add+buy = follow
    ("add",  "sell",  50, False, False),   # add+sell = deviate
    ("trim", "sell",  50, True,  False),   # trim+sell = follow
    ("trim", "buy",  100, False, False),   # trim+buy = deviate
    ("exit", "sell",   0, True,  False),   # exit+清仓 = follow
    ("exit", "sell",  50, False, True),    # exit+部分减仓 = deviate+partial
    ("exit", "buy",  100, False, False),   # exit+buy = deviate
])
def test_matrix_headline(isolated_paths, action, side, shares_after,
                         exp_followed, exp_partial):
    _write_pa(action=action)
    r = _adh(side, fill_price=96.0, shares_after=shares_after)
    assert r is not None
    assert r["action_advice"] == action
    assert r["followed"] is exp_followed
    assert bool(r.get("partial")) is exp_partial
    assert r["stale"] is False      # 7 天 -> fresh
    assert r["source"] == "realtime"


# ── D6 scale_plan：hold 带 ladder 的 trigger 命中评估 ──────────────────────────

def test_hold_scale_buy_hit_add_trigger(isolated_paths):
    """hold + scale_plan + buy@96.85 命中 add@96.8 -> follow + matched_level。"""
    _write_pa(action="hold", scale_plan=_SCALE)
    r = _adh("buy", fill_price=96.85)
    assert r["followed"] is True
    assert r["matched_scale_level"]["trigger_price"] == 96.8
    assert r.get("na_reason") is None


def test_hold_scale_sell_hit_trim_trigger(isolated_paths):
    """hold + scale_plan + sell@86.5 命中 trim@87.0 -> follow + matched_level。"""
    _write_pa(action="hold", scale_plan=_SCALE)
    r = _adh("sell", fill_price=86.5)
    assert r["followed"] is True
    assert r["matched_scale_level"]["trigger_price"] == 87.0


def test_hold_scale_buy_no_hit_na_no_trigger(isolated_paths):
    """hold + scale_plan + buy@95 未命中 add trigger（95 < 96.8）-> na_no_trigger。"""
    _write_pa(action="hold", scale_plan=_SCALE)
    r = _adh("buy", fill_price=95.0)
    assert r["followed"] is None
    assert r["na_reason"] == "na_no_trigger"
    assert r["matched_scale_level"] is None


def test_hold_scale_sell_no_hit_na_no_trigger(isolated_paths):
    """hold + scale_plan + sell@95 未命中 trim trigger（95 > 87）-> na_no_trigger。"""
    _write_pa(action="hold", scale_plan=_SCALE)
    r = _adh("sell", fill_price=95.0)
    assert r["followed"] is None
    assert r["na_reason"] == "na_no_trigger"


def test_hold_empty_scale_na_hold_advice(isolated_paths):
    """hold + 空 scale_plan -> na_hold_advice（无论操作）。"""
    _write_pa(action="hold", scale_plan=[])
    r = _adh("buy", fill_price=96.85)
    assert r["followed"] is None
    assert r["na_reason"] == "na_hold_advice"


def test_hold_missing_scale_plan_key_na_hold_advice(isolated_paths):
    """hold + 缺 scale_plan 键 -> na_hold_advice（与空等价）。"""
    journal.write_entry({
        "ts_code": _TS, "name": "test", "date": "2026-07-20",
        "analyzed_at": "2026-07-20T10:00:00+08:00", "source": "position_action",
        "position_action": {"action": "hold"},  # 无 scale_plan 键
        "verdict": None, "confidence": None, "price_advice": None,
        "features": None, "evidence": None, "analysis_text": "",
    })
    r = _adh("buy", fill_price=96.85)
    assert r["followed"] is None
    assert r["na_reason"] == "na_hold_advice"


# ── null：无 position_action / 开仓无 entry_date ───────────────────────────────

def test_no_position_action_returns_none(isolated_paths):
    """无 position_action -> None（不入统计）。"""
    assert _adh("buy", fill_price=96.0) is None


def test_entry_date_none_returns_none(isolated_paths):
    """开仓 buy 无 entry_date -> None（入场 verdict 不是加减仓建议）。"""
    _write_pa(action="add")
    assert _adh("buy", fill_price=96.0, entry_date=None) is None


# ── D7 cycle 过滤：entry_date <= analyzed_at <= when ───────────────────────────

def test_cycle_filters_pre_entry_pa(isolated_paths):
    """position_action 在 entry_date 之前（上周期建议）-> 过滤掉 -> None。"""
    _write_pa(action="add", analyzed_at="2026-07-05T10:00:00+08:00")  # 早于 entry_date 2026-07-10
    assert _adh("buy", fill_price=96.0) is None


def test_cycle_filters_future_pa(isolated_paths):
    """position_action 在 when 之后 -> 过滤掉 -> None（防御性）。"""
    _write_pa(action="add", analyzed_at="2026-07-28T10:00:00+08:00")  # 晚于 when 2026-07-27
    assert _adh("buy", fill_price=96.0) is None


def test_cycle_picks_latest_in_window(isolated_paths):
    """窗口内多条 position_action -> 取最近一条（analyzed_at 最大）。"""
    _write_pa(action="add", analyzed_at="2026-07-15T10:00:00+08:00")
    _write_pa(action="trim", analyzed_at="2026-07-22T10:00:00+08:00")
    r = _adh("sell", fill_price=96.0)  # trim 建议下 sell = follow
    assert r["action_advice"] == "trim"
    assert r["matched_action_at"] == "2026-07-22T10:00:00+08:00"
    assert r["followed"] is True


# ── stale 边界：14 天整 fresh / 15 天 stale ────────────────────────────────────

def test_stale_14d_exact_is_fresh(isolated_paths):
    """analyzed_at 距 when 整 14 天 -> fresh（stale=False）。"""
    _write_pa(action="add", analyzed_at="2026-07-13T14:00:00+08:00")  # 整 14 天前
    r = _adh("buy", fill_price=96.0)
    assert r["stale"] is False


def test_stale_15d_is_stale(isolated_paths):
    """analyzed_at 距 when 15 天 -> stale=True（仍判 followed，但聚合时入 stale_n）。"""
    _write_pa(action="add", analyzed_at="2026-07-12T14:00:00+08:00")  # 15 天前
    r = _adh("buy", fill_price=96.0)
    assert r["stale"] is True
    assert r["followed"] is True   # add+buy 仍 follow


# ── D3 best-effort：异常不阻断 ─────────────────────────────────────────────────

def test_journal_io_exception_returns_none(isolated_paths, monkeypatch):
    """journal IO 异常 -> None（trade 照常写入，不阻断）。"""
    _write_pa(action="add")

    def _boom(ts_code):
        raise IOError("corrupt journal")
    monkeypatch.setattr("apex.trades.journal.load_position_actions", _boom)
    assert _adh("buy", fill_price=96.0) is None


def test_malformed_scale_plan_na_no_trigger(isolated_paths):
    """scale_plan level 缺 trigger_price（格式异常）-> na_no_trigger（不崩）。"""
    _write_pa(action="hold", scale_plan=[{"level": 1, "action": "add", "shares": 200}])
    r = _adh("buy", fill_price=96.85)
    assert r["followed"] is None
    assert r["na_reason"] == "na_no_trigger"


# ── T4 聚合：_position_action_adherence 全桶 + stale + malformed ────────────────

def _adh_rec(followed, partial=False, stale=False):
    """造一个 adherence dict（aggregation 测试用）。"""
    return {"action_advice": "add", "followed": followed, "partial": partial,
            "stale": stale, "matched_action_at": "", "matched_scale_level": None,
            "source": "realtime"}


def test_aggregation_buckets_and_stale():
    """全桶 + stale 子集 + Q2 fresh n 口径 + follow_rate。"""
    trades_list = [
        {"adherence": _adh_rec(True)},                            # follow fresh
        {"adherence": _adh_rec(True, stale=True)},                # follow stale
        {"adherence": _adh_rec(False)},                           # deviate fresh
        {"adherence": _adh_rec(False, partial=True)},             # partial fresh
        {"adherence": _adh_rec(False, partial=True, stale=True)}, # partial stale
        {"adherence": _adh_rec(None)},                            # na
        {"adherence": None},                                      # null（老 trade）
        {"adherence": "garbage"},                                 # null（malformed）
    ]
    r = _position_action_adherence(trades_list)
    assert r["follow_n"] == 2
    assert r["deviate_n"] == 1
    assert r["partial_n"] == 2
    assert r["na_n"] == 1
    assert r["null_n"] == 2
    assert r["stale_n"] == 2          # 1 follow + 1 partial
    assert r["actionable_n"] == 5     # 2+1+2
    assert r["n"] == 3                # 5 - 2 stale（Q2 排除 stale）
    assert r["follow_rate"] == 0.333  # fresh_follow(1) / fresh_n(3)
    assert r["confidence"] == "low_sample_descriptive_only"


def test_aggregation_no_data():
    """全 null -> no_data。"""
    r = _position_action_adherence([{"adherence": None}, {"adherence": None}])
    assert r["n"] == 0
    assert r["follow_rate"] is None
    assert r["confidence"] == "no_data"


# ── T2/T3 集成：buy/sell 经 append_trade 写入 trade.adherence ───────────────────

def _seed_position(ts_code=_TS, entry_date=_ENTRY, shares=200, avg_cost=96.0):
    """直接写一条现有持仓到 watchlist（绕过 buy 的开仓路径）。"""
    watchlist._save({"active_positions": [{
        "ts_code": ts_code, "name": "中科曙光", "entry_price": avg_cost,
        "avg_cost": avg_cost, "entry_date": entry_date,
        "position_size_shares": shares, "status": "active",
    }], "candidates": [], "archived": []})


def test_buy_add_writes_adherence(isolated_paths, monkeypatch):
    """加仓 buy 命中 add trigger -> trade.adherence.followed=True。"""
    _write_pa(action="hold", scale_plan=_SCALE)
    _seed_position()
    monkeypatch.setattr("apex.watchlist._notify", lambda *a, **k: None)
    res = watchlist.buy(_TS, fill_price=96.85, shares=200)
    adh = res["trade"]["adherence"]
    assert adh["followed"] is True
    assert adh["matched_scale_level"]["trigger_price"] == 96.8
    # 落盘一致
    loaded = trades.load_trades(ts_code=_TS)
    assert loaded[0]["adherence"]["followed"] is True


def test_opening_buy_adherence_null(isolated_paths, monkeypatch):
    """开仓 buy（新票）-> entry_date=None -> adherence=None。"""
    _write_pa(action="add")  # 即便有历史 pa，开仓也不判
    monkeypatch.setattr("apex.watchlist._notify", lambda *a, **k: None)
    monkeypatch.setattr("apex.watchlist._sector_for", lambda ts_code=None: None)
    res = watchlist.buy("002050.SZ", fill_price=25.0, shares=100,
                        stop_loss=23.0, target=30.0)
    assert res["trade"]["adherence"] is None


def test_sell_trim_na_writes_adherence(isolated_paths, monkeypatch):
    """减仓 sell 未命中 trim trigger -> trade.adherence 为 na_no_trigger。"""
    _write_pa(action="hold", scale_plan=_SCALE)  # 只有 add@96.8 / trim@87.0
    _seed_position()
    monkeypatch.setattr("apex.watchlist._notify", lambda *a, **k: None)
    res = watchlist.sell(_TS, fill_price=95.0, shares=100)  # 95 > 87, 未命中 trim
    adh = res["trade"]["adherence"]
    assert adh["followed"] is None
    assert adh["na_reason"] == "na_no_trigger"
