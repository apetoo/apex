"""v1.1.0 B1 T3: record_position_action dispatch 守卫单测。

直接测抽离的 _validate_position_action(tool_input, ts_code, searches_performed) -> (reasons, sizing_warn)。
覆盖：对称守卫（持仓存在性）+ 字段校验 + trim/exit 强制 3 类搜索（OV#1）+ 4h 反 churn（OV#7）+ sizing_cap warn（A1）。
"""
from datetime import datetime, timedelta, timezone

from apex import analyze, journal, watchlist
from apex.analyze import _validate_position_action
from apex.schemas import POSITION_ACTION_SOURCE

_TZ = timezone(timedelta(hours=8))


def _add(ts_code="002050.SZ", shares=1000):
    return watchlist.add_position(ts_code, "test", 12.0, 11.0, 14.0,
                                 position_size_shares=shares)


def _held_position_with_plan():
    return {
        "ts_code": "000977.SZ", "entry_price": 77.095,
        "position_size_shares": 200, "stop_loss": 71.5, "target": 90.0,
        "plan": {"scale_plan": [
            {"level": 1, "action": "trim", "trigger_price": 71.5,
             "pct": 1.0, "reason": "防守退出"},
            {"level": 2, "action": "add", "trigger_price": 79.0,
             "shares": 100, "new_stop": 73.0, "reason": "突破确认"},
        ]},
    }


def _add_held_position_with_plan():
    baseline = _held_position_with_plan()
    watchlist.add_position(
        baseline["ts_code"], "test", baseline["entry_price"], baseline["stop_loss"],
        baseline["target"], position_size_shares=baseline["position_size_shares"],
    )
    watchlist.update_plan(baseline["ts_code"], baseline["plan"])
    return baseline


def test_legacy_empty_ladder_preserves_baseline_in_effective_candidate(isolated_paths):
    baseline = _add_held_position_with_plan()

    proposal, effective, blockers = analyze._prepare_position_action_candidate(
        {"action": "hold", "scale_plan": [], "rationale": "维持原计划"},
        baseline, "000977.SZ", 78.27,
    )

    assert blockers == []
    assert proposal["ladder_intent"] == "preserve"
    assert effective["effective_stop"] == 71.5
    assert effective["effective_target"] == 90.0
    assert len(effective["effective_scale_plan"]) == 2


def test_explicit_clear_and_replace_are_validated_before_review(isolated_paths):
    _add_held_position_with_plan()

    _, _, clear_blockers = analyze._prepare_position_action_candidate(
        {"action": "hold", "ladder_intent": "clear", "rationale": "取消条件单"},
        _held_position_with_plan(), "000977.SZ", 78.27,
    )
    _, _, invalid_blockers = analyze._prepare_position_action_candidate(
        {"action": "hold", "ladder_intent": "replace", "scale_plan": []},
        _held_position_with_plan(), "000977.SZ", 78.27,
    )

    assert clear_blockers == []
    assert any("清空请使用 clear" in item for item in invalid_blockers)


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


# ── 搜索门控由 EvidenceController 统一负责，不再检查类别调用清单 ─────────────

def test_trim_does_not_use_legacy_search_checklist(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "trim_shares": 100, "rationale": "x"}, "002050.SZ", []
    )
    assert _ok(reasons)


def test_trim_ignores_partial_legacy_search_list(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "trim_shares": 100, "rationale": "x"}, "002050.SZ",
        ["regulatory", "shareholders", "earnings"],
    )
    assert _ok(reasons)


def test_trim_all_three_search_ok(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "trim", "trim_shares": 100, "rationale": "x"}, "002050.SZ",
        ["regulatory", "shareholders", "money_flow"],
    )
    assert _ok(reasons)


def test_exit_does_not_use_legacy_search_checklist(isolated_paths):
    _add()
    reasons, _ = _validate_position_action(
        {"action": "exit", "rationale": "x"}, "002050.SZ", []
    )
    assert _ok(reasons)


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


# ── 守卫 5 (ladder 自洽): trim 档 trigger_price ≥ 有效止损 ────────────────────
# 死代码防护：trim 触发价 < 有效止损（new_stop 或持仓 stop_loss）-> 止损先全仓离场，
# 该减仓档永不执行。headline action=hold + scale_plan 复刻 601138 场景（hold 但 ladder 带未来 trim）。
# _add() 建仓 stop_loss=11.0。

def test_trim_trigger_below_new_stop_rejected(isolated_paths):
    """new_stop=12.0 时 trim@11.5 是死代码（11.5 < 12.0）-> 拒。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "new_stop": 12.0, "rationale": "x",
         "scale_plan": [{"level": 1, "action": "trim", "trigger_price": 11.5, "shares": 100}]},
        "002050.SZ", [],
    )
    assert reasons
    assert any("死代码" in r or "低于有效止损" in r for r in reasons)


def test_trim_trigger_below_held_stop_rejected(isolated_paths):
    """无 new_stop 时用持仓 stop_loss=11.0，trim@10.5 是死代码 -> 拒。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [{"level": 1, "action": "trim", "trigger_price": 10.5, "shares": 100}]},
        "002050.SZ", [],
    )
    assert reasons
    assert any("死代码" in r for r in reasons)


def test_trim_trigger_above_new_stop_ok(isolated_paths):
    """trim@13.0 > new_stop 12.0 -> 自洽，过。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "new_stop": 12.0, "rationale": "x",
         "scale_plan": [{"level": 1, "action": "trim", "trigger_price": 13.0, "shares": 100}]},
        "002050.SZ", [],
    )
    assert _ok(reasons)


def test_trim_trigger_equal_stop_ok(isolated_paths):
    """trim@11.0 == new_stop 11.0 -> 边界不拦（严格 <，== 留给 AI 语义裁量）。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "new_stop": 11.0, "rationale": "x",
         "scale_plan": [{"level": 1, "action": "trim", "trigger_price": 11.0, "shares": 100}]},
        "002050.SZ", [],
    )
    assert _ok(reasons)


def test_trim_level_missing_trigger_skipped(isolated_paths):
    """trim 档缺 trigger_price -> 跳过该校验（不崩、不拒；adherence eval 也跳 None trigger）。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [{"level": 1, "action": "trim", "shares": 100}]},
        "002050.SZ", [],
    )
    assert _ok(reasons)


def test_trim_trigger_non_numeric_skipped(isolated_paths):
    """trim 档 trigger_price 非数 -> 跳过（不崩）。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [{"level": 1, "action": "trim", "trigger_price": "破位", "shares": 100}]},
        "002050.SZ", [],
    )
    assert _ok(reasons)


def test_add_level_below_stop_not_flagged(isolated_paths):
    """add 档 trigger 低于止损不拦（守卫 5 只管 trim 死代码；add 档 staleness 是另一类问题）。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [{"level": 1, "action": "add", "trigger_price": 9.0, "shares": 100}]},
        "002050.SZ", [],
    )
    assert _ok(reasons)


def test_multiple_trim_one_below_stop_rejected(isolated_paths):
    """多档 trim：一档自洽一档死代码 -> 整体拒，且报第几档。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "new_stop": 12.0, "rationale": "x",
         "scale_plan": [
             {"level": 1, "action": "trim", "trigger_price": 13.0, "shares": 100},
             {"level": 2, "action": "trim", "trigger_price": 10.0, "shares": 100},
         ]},
        "002050.SZ", [],
    )
    assert reasons
    assert any("第 2 档" in r for r in reasons)


# ── 守卫 6 (ladder 跨档自洽, advisory): add 档 new_stop 不得让 trim 档变死代码 ──
# add 执行后止损上移，低于新止损的 trim 档永不触发。不拒（trim 在加仓前可能仍有防御价值），
# 但警告逼 AI 在 trim 档 reason 注明失效条件。复刻 002415 场景（trim@34.6 + add new_stop=35.5）。
# _add() 建仓 stop_loss=11.0。

def test_add_new_stop_orphans_trim_warns_not_rejects(isolated_paths):
    """trim@11.5 + add new_stop=12.0 -> 加仓后 trim 永不触发：不拒但 advisory 警告，报第几档。"""
    _add()
    reasons, warn = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [
             {"level": 1, "action": "trim", "trigger_price": 11.5, "shares": 100},
             {"level": 1, "action": "add", "trigger_price": 13.0, "shares": 100, "new_stop": 12.0},
         ]},
        "002050.SZ", [],
    )
    assert _ok(reasons)  # advisory 不拒
    assert "跨档自洽" in warn
    assert "第 1 档 trim@11.5" in warn and "第 2 档 add" in warn


def test_add_new_stop_below_trim_trigger_no_warn(isolated_paths):
    """add new_stop=11.2 < trim@11.5 -> 加仓后 trim 仍有效，无警告。"""
    _add()
    reasons, warn = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [
             {"level": 1, "action": "trim", "trigger_price": 11.5, "shares": 100},
             {"level": 1, "action": "add", "trigger_price": 13.0, "shares": 100, "new_stop": 11.2},
         ]},
        "002050.SZ", [],
    )
    assert _ok(reasons)
    assert warn == ""


def test_add_level_without_new_stop_no_warn(isolated_paths):
    """add 档缺 new_stop -> 跳过跨档校验（不崩、不警告）。"""
    _add()
    reasons, warn = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [
             {"level": 1, "action": "trim", "trigger_price": 11.5, "shares": 100},
             {"level": 1, "action": "add", "trigger_price": 13.0, "shares": 100},
         ]},
        "002050.SZ", [],
    )
    assert _ok(reasons)
    assert warn == ""


def test_multiple_adds_killing_same_trim_reported_once(isolated_paths):
    """两档 add 的 new_stop 都高于同一 trim -> 只报一次，指向最低（最先生效）的杀手档。"""
    _add()
    reasons, warn = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [
             {"level": 1, "action": "trim", "trigger_price": 11.5, "shares": 100},
             {"level": 1, "action": "add", "trigger_price": 13.0, "shares": 100, "new_stop": 12.0},
             {"level": 2, "action": "add", "trigger_price": 13.8, "shares": 100, "new_stop": 13.5},
         ]},
        "002050.SZ", [],
    )
    assert _ok(reasons)
    assert warn.count("trim@11.5") == 1
    assert "止损上移至 12" in warn  # min killer = L1 add 的 12.0，而非 L2 的 13.5


# ── 守卫 7 (止盈-加仓自洽): add 档 trigger_price < 有效止盈价 ────────────────
# 到止盈价触发止盈提醒/离场，又在同等或更高价加仓 = 计划自相矛盾（002415 场景：
# 化石 target=38.0 vs ladder add@38.2）。有效止盈 = new_target（若给）否则持仓 target。
# _add() 建仓 target=14.0。

def test_add_trigger_above_held_target_rejected(isolated_paths):
    """add@14.5 > 持仓 target 14.0 -> 止盈价之上加仓，拒，提示 new_target 修复路径。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [{"level": 1, "action": "add", "trigger_price": 14.5, "shares": 100}]},
        "002050.SZ", [],
    )
    assert reasons
    assert any("止盈" in r and "new_target" in r for r in reasons)


def test_add_trigger_equal_target_rejected(isolated_paths):
    """add@14.0 == target 14.0 -> 边界也拒（到价同时止盈+加仓仍矛盾）。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [{"level": 1, "action": "add", "trigger_price": 14.0, "shares": 100}]},
        "002050.SZ", [],
    )
    assert reasons


def test_add_trigger_below_target_ok(isolated_paths):
    """add@13.5 < target 14.0 -> 自洽，过。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [{"level": 1, "action": "add", "trigger_price": 13.5, "shares": 100}]},
        "002050.SZ", [],
    )
    assert _ok(reasons)


def test_new_target_overrides_held_target(isolated_paths):
    """add@14.5 ≥ 持仓 target 14.0，但同调 new_target=16.0 -> 有效止盈 16.0，自洽通过。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "new_target": 16.0, "rationale": "x",
         "scale_plan": [{"level": 1, "action": "add", "trigger_price": 14.5, "shares": 100}]},
        "002050.SZ", [],
    )
    assert _ok(reasons)


def test_trim_above_target_not_flagged(isolated_paths):
    """trim@15.0 > target 14.0 不拦（止盈之上的减仓=继续兑现，良性；守卫 7 只管 add 方向）。"""
    _add()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x",
         "scale_plan": [{"level": 1, "action": "trim", "trigger_price": 15.0, "shares": 100}]},
        "002050.SZ", [],
    )
    assert _ok(reasons)
