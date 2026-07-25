"""v1.1.0 B1 T4: 持仓 ladder（active_positions.plan）生命周期。

CRITICAL 回归：close_position null 掉 archived 面包屑的 plan + 重开同 ts_code 拿到的是
fresh 空 plan，不是陈旧 ladder（防 close+reopen 读到旧 ladder）。
"""
from apex import watchlist


def _add(ts_code="002050.SZ", shares=1000):
    return watchlist.add_position(ts_code, "test", 12.0, 11.0, 14.0,
                                 position_size_shares=shares)


# ── _empty_plan / 开仓初始化 ─────────────────────────────────────────────────

def test_empty_plan_shape():
    p = watchlist._empty_plan()
    assert p == {"scale_plan": [], "doctrine": "single_v1", "updated_at": None, "last_action": None, "last_new_stop": None, "last_stop_before": None}


def test_add_position_initializes_empty_plan(isolated_paths):
    rec = _add()
    assert rec["plan"] == {"scale_plan": [], "doctrine": "single_v1", "updated_at": None, "last_action": None, "last_new_stop": None, "last_stop_before": None}
    # 落盘后 reload 仍在
    wl = watchlist.load()
    assert wl["active_positions"][0]["plan"]["scale_plan"] == []


def test_buy_new_position_initializes_empty_plan(isolated_paths):
    """buy() 开新仓分支也初始化 plan（add_position 之外的咽喉）。"""
    from apex import trades  # noqa: F401  (buy 内部 import)
    res = watchlist.buy("002050.SZ", fill_price=12.0, shares=500,
                        stop_loss=11.0, target=14.0)
    assert res["position"]["plan"]["scale_plan"] == []


# ── update_plan ──────────────────────────────────────────────────────────────

def test_update_plan_refreshes_ladder(isolated_paths):
    _add()
    new_plan = {
        "scale_plan": [
            {"level": 1, "trigger_price": 13.0, "action": "trim", "pct": 0.33, "new_stop": 12.0},
        ],
        "doctrine": "single_v1", "updated_at": "2026-07-05T10:00:00+08:00",
    }
    updated = watchlist.update_plan("002050.SZ", new_plan)
    assert updated is not None
    assert len(updated["plan"]["scale_plan"]) == 1
    # reload 确认落盘
    wl = watchlist.load()
    assert wl["active_positions"][0]["plan"]["scale_plan"][0]["trigger_price"] == 13.0


def test_update_plan_missing_position_returns_none(isolated_paths):
    """竞态：分析期间已平仓 -> plan 无处可写，返回 None（不抛）。"""
    assert watchlist.update_plan("999999.SZ", {"scale_plan": [], "doctrine": "single_v1"}) is None


def test_update_plan_stamps_last_stop_before_from_position_stop(isolated_paths):
    """B1 增强：plan 带 last_action 时，update_plan 从持仓当前 stop_lock 补 last_stop_before（race-free，单次 load）。"""
    _add()  # stop_loss=11.0
    new_plan = {
        "scale_plan": [], "doctrine": "single_v1", "updated_at": "2026-07-05T10:00:00+08:00",
        "last_action": "hold", "last_new_stop": 11.5,
    }
    updated = watchlist.update_plan("002050.SZ", new_plan)
    assert updated is not None
    assert updated["plan"]["last_action"] == "hold"
    assert updated["plan"]["last_new_stop"] == 11.5
    assert updated["plan"]["last_stop_before"] == 11.0  # 从持仓 stop_loss 补
    # 入参 dict 不被原地改（plan_to_write 是 copy）
    assert "last_stop_before" not in new_plan


def test_update_plan_no_last_action_does_not_stamp(isolated_paths):
    """plan 无 last_action（非 position_action 快照）-> 不补 last_stop_before（向后兼容老 plan）。"""
    _add()
    new_plan = {"scale_plan": [], "doctrine": "single_v1", "updated_at": "x"}
    updated = watchlist.update_plan("002050.SZ", new_plan)
    assert updated is not None
    assert "last_stop_before" not in updated["plan"]


# ── CRITICAL 回归：close null plan + 重开 fresh ──────────────────────────────

def test_critical_close_nulls_plan_and_reopen_is_fresh(isolated_paths):
    """CRITICAL: close 把 archived 面包屑的 plan null 掉；重开同 ts_code 拿 fresh 空 plan。

    防同 ts_code 重开读到陈旧 ladder（OV#13 close-then-reopen 的 plan 侧）。
    """
    _add()
    # 先把 plan 填充成有内容的 ladder
    populated = {
        "scale_plan": [
            {"level": 1, "trigger_price": 13.0, "action": "trim", "pct": 0.5},
            {"level": 2, "trigger_price": 15.0, "action": "trim", "pct": 0.5},
        ],
        "doctrine": "single_v1", "updated_at": "2026-07-05T10:00:00+08:00",
    }
    watchlist.update_plan("002050.SZ", populated)
    assert len(watchlist.load()["active_positions"][0]["plan"]["scale_plan"]) == 2

    # 平仓 -> active 移除，archived 面包屑 plan 被 null
    watchlist.close_position("002050.SZ", exit_price=13.5, exit_reason="target_hit",
                             record_trade=False)
    wl = watchlist.load()
    assert not wl["active_positions"]  # 已移除
    archived = [a for a in wl["archived"] if a.get("ts_code") == "002050.SZ"]
    assert archived and archived[0]["plan"] is None  # null 掉，不残留陈旧 ladder

    # 重开同 ts_code -> fresh 空 plan，不是之前填充的 2 档 ladder
    _add()
    wl = watchlist.load()
    assert len(wl["active_positions"]) == 1
    plan = wl["active_positions"][0]["plan"]
    assert plan["scale_plan"] == []  # fresh，非陈旧
    assert plan["doctrine"] == "single_v1"
