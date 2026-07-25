"""v1.1.0 B1 T3: record_position_action dispatch 守卫单测。

直接测抽离的 _validate_position_action(tool_input, ts_code, searches_performed) -> (reasons, sizing_warn)。
覆盖：对称守卫（持仓存在性）+ 字段校验 + trim/exit 强制 3 类搜索（OV#1）+ 4h 反 churn（OV#7）+ sizing_cap warn（A1）。
"""
from datetime import datetime, timedelta, timezone

from apex import journal, watchlist
from apex.analyze import _validate_position_action
from apex.schemas import POSITION_ACTION_SOURCE

_TZ = timezone(timedelta(hours=8))


def _add(ts_code="002050.SZ", shares=1000):
    return watchlist.add_position(ts_code, "test", 12.0, 11.0, 14.0,
                                 position_size_shares=shares)


def _write_prior_pa(ts_code, hours_ago):
    """写一条 hours_ago 小时前的 position_action entry（4h 反 churn 基线）。"""
    at = (datetime.now(_TZ) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    journal.write_entry({
        "ts_code": ts_code, "name": "test",
        "date": at[:10], "analyzed_at": at,
        "source": POSITION_ACTION_SOURCE,
        "position_action": {"action": "hold", "rationale": "prior", "scale_plan": []},
        "analysis_text": "", "verdict": None, "confidence": None,
        "price_advice": None, "features": None, "evidence": None,
    })


def _ok(reasons, sizing_warn=""):
    """断言接受：reasons 空（sizing_warn 可非空 = advisory 警告仍接受）。"""
    return reasons == []


# ── 守卫 1: 持仓存在性（对称守卫）──────────────────────────────────────────

def test_not_held_rejected(isolated_paths):
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x"}, "999999.SZ", []
    )
    assert reasons  # 拒绝
    assert any("已不持仓" in r for r in reasons)


def test_held_accepted(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x"}, "002050.SZ", []
    )
    assert _ok(reasons)


# ── 守卫 2: 字段校验 ─────────────────────────────────────────────────────────

def test_add_valid(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "add", "add_shares": 300, "rationale": "x"}, "002050.SZ", []
    )
    assert _ok(reasons)


def test_add_zero_shares_rejected(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "add", "add_shares": 0, "rationale": "x"}, "002050.SZ", []
    )
    assert reasons


def test_add_missing_shares_rejected(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "add", "rationale": "x"}, "002050.SZ", []
    )
    assert reasons


def test_add_negative_shares_rejected(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "add", "add_shares": -5, "rationale": "x"}, "002050.SZ", []
    )
    assert reasons


def test_trim_neither_rejected(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "rationale": "x"}, "002050.SZ",
        ["regulatory", "shareholders", "money_flow"],
    )
    assert reasons


def test_trim_both_rejected(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "trim_shares": 100, "trim_pct": 0.3, "rationale": "x"},
        "002050.SZ", ["regulatory", "shareholders", "money_flow"],
    )
    assert reasons


def test_trim_valid_shares(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "trim_shares": 200, "rationale": "x"}, "002050.SZ",
        ["regulatory", "shareholders", "money_flow"],
    )
    assert _ok(reasons)


def test_trim_valid_pct(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "trim_pct": 0.33, "rationale": "x"}, "002050.SZ",
        ["regulatory", "shareholders", "money_flow"],
    )
    assert _ok(reasons)


def test_trim_pct_boundary_one_ok(isolated_paths):
    """trim_pct=1.0（全减）边界 (0,1] 接受。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "trim_pct": 1.0, "rationale": "x"}, "002050.SZ",
        ["regulatory", "shareholders", "money_flow"],
    )
    assert _ok(reasons)


def test_trim_pct_over_one_rejected(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "trim_pct": 1.5, "rationale": "x"}, "002050.SZ",
        ["regulatory", "shareholders", "money_flow"],
    )
    assert reasons


def test_hold_valid(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "new_stop": 12.5, "rationale": "x"}, "002050.SZ", []
    )
    assert _ok(reasons)


def test_invalid_action_rejected(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "buy", "rationale": "x"}, "002050.SZ", []
    )
    assert reasons


# ── 守卫 3 (OV#1): trim/exit 强制 3 类搜索 ───────────────────────────────────

def test_trim_mandatory_search_missing_rejected(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "trim_shares": 100, "rationale": "x"}, "002050.SZ", []
    )
    assert reasons
    assert any("regulatory" in r or "shareholders" in r or "money_flow" in r for r in reasons)


def test_trim_partial_search_rejected(isolated_paths):
    """缺 money_flow -> 拒。earnings 不强制。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "trim_shares": 100, "rationale": "x"}, "002050.SZ",
        ["regulatory", "shareholders", "earnings"],
    )
    assert reasons


def test_trim_all_three_search_ok(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "trim_shares": 100, "rationale": "x"}, "002050.SZ",
        ["regulatory", "shareholders", "money_flow"],
    )
    assert _ok(reasons)


def test_exit_mandatory_search_missing_rejected(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "exit", "rationale": "x"}, "002050.SZ", []
    )
    assert reasons


def test_exit_all_three_search_ok(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "exit", "rationale": "x"}, "002050.SZ",
        ["regulatory", "shareholders", "money_flow"],
    )
    assert _ok(reasons)


def test_hold_no_mandatory_search(isolated_paths):
    """hold 不涉实质风险决策，不强制搜索。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x"}, "002050.SZ", []
    )
    assert _ok(reasons)


# ── 守卫 4 (OV#7): 4h 反 churn ───────────────────────────────────────────────

def test_churn_within_4h_no_new_info_rejected(isolated_paths):
    _add()
    _write_prior_pa("002050.SZ", hours_ago=2)
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x"}, "002050.SZ", []
    )
    assert reasons
    assert any("4h" in r or "churn" in r for r in reasons)


def test_churn_within_4h_with_new_info_ok(isolated_paths):
    _add()
    _write_prior_pa("002050.SZ", hours_ago=2)
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x", "new_info": ["放量突破"]}, "002050.SZ", []
    )
    assert _ok(reasons)


def test_churn_beyond_4h_ok(isolated_paths):
    _add()
    _write_prior_pa("002050.SZ", hours_ago=5)
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x"}, "002050.SZ", []
    )
    assert _ok(reasons)


def test_churn_no_prior_pa_ok(isolated_paths):
    """无 prior position_action -> 不触发反 churn。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x"}, "002050.SZ", []
    )
    assert _ok(reasons)


def test_churn_prior_verdict_does_not_trigger(isolated_paths):
    """反 churn 基线是 position_action，不是 verdict -- prior verdict 不应触发 4h 限制。"""
    _add()
    journal.write_entry({
        "ts_code": "002050.SZ", "name": "test",
        "date": "2026-07-01", "analyzed_at": (datetime.now(_TZ) - timedelta(hours=1)).isoformat(timespec="seconds"),
        "verdict": "看多", "confidence": 7, "source": "standalone",
        "price_advice": {}, "features": {}, "evidence": [], "analysis_text": "",
    })
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x"}, "002050.SZ", []
    )
    assert _ok(reasons)


# ── A1 sizing_cap warn（advisory，不拒）──────────────────────────────────────

def test_sizing_cap_warn_when_over_limit(isolated_paths, monkeypatch):
    """加仓后总风险超上限 -> reasons 空（接受）+ sizing_warn 非空（advisory 警告）。"""
    _add()
    monkeypatch.setattr(
        "apex.account.current_total_risk",
        lambda positions, account=None: {
            "over_limit": True, "total_risk_pct": 15.0, "max_total_risk_pct": 10.0,
        },
    )
    monkeypatch.setattr("apex.account.load", lambda: {})
    reasons, sizing_warn = _validate_position_action(
        {"action": "add", "add_shares": 99999, "rationale": "x"}, "002050.SZ", []
    )
    assert _ok(reasons)  # 不拒
    assert sizing_warn  # 有警告
    assert "超上限" in sizing_warn


def test_sizing_cap_no_warn_when_under_limit(isolated_paths, monkeypatch):
    _add()
    monkeypatch.setattr(
        "apex.account.current_total_risk",
        lambda positions, account=None: {"over_limit": False, "total_risk_pct": 5.0, "max_total_risk_pct": 10.0},
    )
    monkeypatch.setattr("apex.account.load", lambda: {})
    reasons, sizing_warn = _validate_position_action(
        {"action": "add", "add_shares": 100, "rationale": "x"}, "002050.SZ", []
    )
    assert _ok(reasons)
    assert sizing_warn == ""
