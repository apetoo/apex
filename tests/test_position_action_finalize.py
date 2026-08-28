"""v1.1.0 B1 T3 端到端：_finalize_position_action 收尾（持仓路径）。

验证 position_action journal entry 形状（P1：verdict/price_advice/features 全 None，
source=position_action）+ active_positions.plan 刷新 + playstyle 从最近 verdict 继承（OV#3）。
mock 掉 data.get_name_map（网络），其余真实跑。
"""
from unittest.mock import patch

from apex import journal, watchlist
from apex.analyze import (
    _finalize_position_action,
    materialize_effective_position_plan,
)
from apex.schemas import POSITION_ACTION_SOURCE


def _add(ts_code="002050.SZ", shares=1000):
    return watchlist.add_position(ts_code, "test", 12.0, 11.0, 14.0,
                                 position_size_shares=shares)


def _finalize(ts_code, proposal, analysis_text="text", *, save=True):
    baseline = next(
        position for position in watchlist.load()["active_positions"]
        if position["ts_code"] == ts_code
    )
    effective = materialize_effective_position_plan(baseline, proposal)
    return _finalize_position_action(
        ts_code, proposal, effective, analysis_text, [], {}, {}, save=save,
    )


def _add_position_with_two_level_plan():
    _add(shares=200)
    watchlist.update_plan("002050.SZ", {
        "doctrine": "single_v1",
        "scale_plan": [
            {"level": 1, "action": "trim", "trigger_price": 11.0, "pct": 1.0},
            {"level": 2, "action": "add", "trigger_price": 13.0,
             "shares": 100, "new_stop": 11.5},
        ],
    })
    return watchlist.load()["active_positions"][0]


def _write_verdict_with_playstyle(ts_code):
    journal.write_entry({
        "ts_code": ts_code, "name": "test",
        "date": "2026-07-01", "analyzed_at": "2026-07-01T10:00:00+08:00",
        "verdict": "看多", "confidence": 7, "source": "standalone",
        "price_advice": {"entry": 12.0, "stop_loss": 11.0, "target": 14.0},
        "features": {}, "evidence": [], "analysis_text": "",
        "playstyle": {"ratings": {"打野": 0, "波段": 5, "中线": 3, "长线": 2},
                      "primary": "波段", "reasons": ["x"]},
    })


@patch("apex.data.get_name_map")
def test_finalize_writes_position_action_entry_and_refreshes_plan(mock_names, isolated_paths):
    mock_names.return_value = {}
    _add()
    proposal = {
        "action": "add",
        "add_shares": 300,
        "new_stop": 12.2,
        "ladder_intent": "replace",
        "scale_plan": [
            {"level": 1, "trigger_price": 13.0, "action": "trim", "pct": 0.33, "new_stop": 12.5},
        ],
        "rationale": "突破确认加仓",
    }
    entry = _finalize("002050.SZ", proposal, "analysis text")

    # P1: verdict 类字段全 None，source=position_action
    assert entry["source"] == POSITION_ACTION_SOURCE
    assert entry["verdict"] is None
    assert entry["confidence"] is None
    assert entry["price_advice"] is None
    assert entry["analysis_status"] == "completed"
    assert entry["evidence"] == []
    # position_action 块
    assert entry["position_action"]["action"] == "add"
    assert entry["position_action"]["add_shares"] == 300
    assert len(entry["position_action"]["scale_plan"]) == 1

    # journal 落盘：position_actions 可读，verdicts 不含它（不污染）
    pas = journal.load_position_actions("002050.SZ")
    assert len(pas) == 1
    assert pas[0]["position_action"]["action"] == "add"
    assert journal.load_verdicts("002050.SZ") == []

    # active_positions.plan 被刷新
    wl = watchlist.load()
    plan = wl["active_positions"][0]["plan"]
    assert len(plan["scale_plan"]) == 1
    assert plan["scale_plan"][0]["trigger_price"] == 13.0
    assert plan["doctrine"] == "single_v1"
    assert plan["updated_at"] == entry["analyzed_at"]
    # B1 增强：plan 带 position_action 快照（last_stop_before 由 update_plan 从持仓 stop_lock 补, race-free）
    assert plan["last_action"] == "add"
    assert plan["last_new_stop"] == 12.2
    assert plan["last_stop_before"] == 11.0  # 持仓 stop_loss（_add 传入）
    # B1: new_stop 自动覆盖持仓 stop_loss（AI 建议即生效，不等 B3 sim）
    assert wl["active_positions"][0]["stop_loss"] == 12.2


@patch("apex.data.get_name_map")
def test_finalize_closed_position_returns_race_result_without_any_write(mock_names, isolated_paths):
    """A closed position invalidates the frozen baseline before journal/watchlist mutation."""
    mock_names.return_value = {}
    baseline = _add()
    watchlist.close_position("002050.SZ", exit_price=13.0, exit_reason="manual",
                             record_trade=False)
    proposal = {"action": "hold", "ladder_intent": "preserve", "rationale": "x"}
    effective = materialize_effective_position_plan(baseline, proposal)
    result = _finalize_position_action(
        "002050.SZ", proposal, effective, "text", [], {}, {}, save=True,
        position_baseline=baseline,
    )

    assert result["analysis_status"] == "insufficient_evidence"
    assert result["outcome_reason"] == "position_changed_during_analysis"
    assert journal.load_position_actions("002050.SZ") == []
    assert watchlist.load()["active_positions"] == []


@patch("apex.data.get_name_map")
def test_finalize_changed_trading_state_returns_race_result_without_any_write(mock_names, isolated_paths):
    """Only trading fields participate in the save-time frozen-baseline comparison."""
    mock_names.return_value = {}
    baseline = _add_position_with_two_level_plan()
    proposal = {"action": "hold", "ladder_intent": "clear", "rationale": "取消条件单"}
    effective = materialize_effective_position_plan(baseline, proposal)
    watchlist.update_position("002050.SZ", stop_loss=10.8)

    result = _finalize_position_action(
        "002050.SZ", proposal, effective, "report", [], {}, {}, save=True,
        position_baseline=baseline,
    )

    position = watchlist.load()["active_positions"][0]
    assert result["outcome_reason"] == "position_changed_during_analysis"
    assert journal.load_position_actions("002050.SZ") == []
    assert position["stop_loss"] == 10.8
    assert len(position["plan"]["scale_plan"]) == 2


@patch("apex.data.get_name_map")
def test_finalize_ignores_non_trading_metadata_in_race_comparison(mock_names, isolated_paths):
    mock_names.return_value = {}
    baseline = _add_position_with_two_level_plan()
    proposal = {"action": "hold", "ladder_intent": "clear", "rationale": "取消条件单"}
    effective = materialize_effective_position_plan(baseline, proposal)
    watchlist.update_position("002050.SZ", name="改名不影响交易计划")

    entry = _finalize_position_action(
        "002050.SZ", proposal, effective, "report", [], {}, {}, save=True,
        position_baseline=baseline,
    )

    assert entry["analysis_status"] == "completed"
    assert len(journal.load_position_actions("002050.SZ")) == 1
    assert watchlist.load()["active_positions"][0]["plan"]["scale_plan"] == []


@patch("apex.data.get_name_map")
def test_finalize_inherits_playstyle_from_latest_verdict(mock_names, isolated_paths):
    """OV#3: position_action 路径捕获 playstyle -- 从最近 verdict 继承（股票属性，稳定）。"""
    mock_names.return_value = {}
    _write_verdict_with_playstyle("002050.SZ")  # prior verdict 带 playstyle
    _add()
    entry = _finalize("002050.SZ", {
        "action": "hold", "ladder_intent": "preserve", "rationale": "x",
    })
    # playstyle 例外（非 None），继承自 prior verdict
    assert entry["playstyle"] is not None
    assert entry["playstyle"]["primary"] == "波段"
    # 而 verdict/price_advice 仍 None（不污染 calibration/backtest）
    assert entry["verdict"] is None
    assert entry["price_advice"] is None


@patch("apex.data.get_name_map")
def test_finalize_new_stop_none_leaves_stop_loss_unchanged(mock_names, isolated_paths):
    """new_stop=None（AI 未调止损）-> 不动持仓 stop_loss（也不动 plan 快照的 stop 字段）。"""
    mock_names.return_value = {}
    _add()  # stop_loss=11.0
    entry = _finalize("002050.SZ", {
        "action": "hold", "ladder_intent": "preserve", "rationale": "x",
    })
    assert entry["position_action"]["new_stop"] is None
    pos = watchlist.load()["active_positions"][0]
    assert pos["stop_loss"] == 11.0  # 未动
    assert pos["plan"]["last_new_stop"] is None
    assert pos["plan"]["last_stop_before"] == 11.0  # 仍记旧 stop（供 delta 展示，即使未变）


@patch("apex.data.get_name_map")
def test_finalize_preserve_keeps_watchlist_and_records_effective_state(mock_names, isolated_paths):
    """A preserve proposal must retain state without misreporting it as new advice."""
    mock_names.return_value = {}
    baseline = _add_position_with_two_level_plan()
    proposal = {"action": "hold", "ladder_intent": "preserve", "rationale": "维持"}
    effective = materialize_effective_position_plan(baseline, proposal)

    entry = _finalize_position_action(
        "002050.SZ", proposal, effective, "report", [], {}, {}, save=True,
    )

    position = watchlist.load()["active_positions"][0]
    assert position["stop_loss"] == 11.0
    assert position["target"] == 14.0
    assert position["plan"]["scale_plan"] == effective["effective_scale_plan"]
    assert entry["position_action"]["new_stop"] is None
    assert entry["position_action"]["new_target"] is None
    assert entry["position_action"]["ladder_intent"] == "preserve"
    assert entry["position_action"]["effective_stop"] == 11.0
    assert entry["position_action"]["effective_target"] == 14.0
    assert entry["position_action"]["scale_plan"] == effective["effective_scale_plan"]


@patch("apex.data.get_name_map")
def test_finalize_clear_removes_only_ladder(mock_names, isolated_paths):
    """An explicit clear changes only the ladder, never the inherited stop or target."""
    mock_names.return_value = {}
    baseline = _add_position_with_two_level_plan()
    proposal = {"action": "hold", "ladder_intent": "clear", "rationale": "取消条件单"}
    effective = materialize_effective_position_plan(baseline, proposal)

    _finalize_position_action(
        "002050.SZ", proposal, effective, "report", [], {}, {}, save=True,
    )

    position = watchlist.load()["active_positions"][0]
    assert position["plan"]["scale_plan"] == []
    assert position["stop_loss"] == 11.0
    assert position["target"] == 14.0


@patch("apex.data.get_name_map")
def test_finalize_persists_the_same_canonical_full_exit_ladder_everywhere(mock_names, isolated_paths):
    mock_names.return_value = {}
    baseline = _add_position_with_two_level_plan()
    proposal = {
        "action": "hold", "ladder_intent": "replace", "rationale": "跌破后全部退出",
        "scale_plan": [{
            "action": "trim", "trigger_price": 11.0, "pct": 1.0, "new_stop": 0,
        }],
    }
    effective = materialize_effective_position_plan(baseline, proposal)

    entry = _finalize_position_action(
        "002050.SZ", proposal, effective, "report", [], {}, {}, save=True,
        position_baseline=baseline,
    )

    scale_plan = effective["effective_scale_plan"]
    assert scale_plan[0]["new_stop"] is None
    assert entry["position_action"]["scale_plan"] == scale_plan
    assert watchlist.load()["active_positions"][0]["plan"]["scale_plan"] == scale_plan


@patch("apex.data.get_name_map")
def test_finalize_applies_only_explicit_stop_and_target_recommendations(mock_names, isolated_paths):
    """A proposal's explicit advice updates once; inherited effective values do not."""
    mock_names.return_value = {}
    baseline = _add_position_with_two_level_plan()
    proposal = {
        "action": "hold", "ladder_intent": "preserve", "new_stop": 11.8,
        "new_target": 14.8, "rationale": "上移保护位",
    }
    effective = materialize_effective_position_plan(baseline, proposal)

    entry = _finalize_position_action(
        "002050.SZ", proposal, effective, "report", [], {}, {}, save=True,
    )

    position = watchlist.load()["active_positions"][0]
    assert position["stop_loss"] == 11.8
    assert position["target"] == 14.8
    assert entry["position_action"]["new_stop"] == 11.8
    assert entry["position_action"]["new_target"] == 14.8
    assert entry["position_action"]["effective_stop"] == 11.8
    assert entry["position_action"]["effective_target"] == 14.8
