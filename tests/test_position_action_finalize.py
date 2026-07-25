"""v1.1.0 B1 T3 端到端：_finalize_position_action 收尾（持仓路径）。

验证 position_action journal entry 形状（P1：verdict/price_advice/features 全 None，
source=position_action）+ active_positions.plan 刷新 + playstyle 从最近 verdict 继承（OV#3）。
mock 掉 data.get_name_map（网络），其余真实跑。
"""
from unittest.mock import patch

from apex import journal, watchlist
from apex.analyze import _finalize_position_action
from apex.schemas import POSITION_ACTION_SOURCE


def _add(ts_code="002050.SZ", shares=1000):
    return watchlist.add_position(ts_code, "test", 12.0, 11.0, 14.0,
                                 position_size_shares=shares)


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
    pa_data = {
        "action": "add",
        "add_shares": 300,
        "new_stop": 12.2,
        "scale_plan": [
            {"level": 1, "trigger_price": 13.0, "action": "trim", "pct": 0.33, "new_stop": 12.5},
        ],
        "rationale": "突破确认加仓",
    }
    entry = _finalize_position_action(
        "002050.SZ", pa_data, "analysis text", [], {}, {}, save=True,
    )

    # P1: verdict 类字段全 None，source=position_action
    assert entry["source"] == POSITION_ACTION_SOURCE
    assert entry["verdict"] is None
    assert entry["confidence"] is None
    assert entry["price_advice"] is None
    assert entry["evidence"] is None
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
def test_finalize_plan_not_written_when_position_closed(mock_names, isolated_paths):
    """竞态：分析期间平仓 -> plan 无处可写，但 journal position_action 仍落盘。"""
    mock_names.return_value = {}
    _add()
    watchlist.close_position("002050.SZ", exit_price=13.0, exit_reason="manual",
                             record_trade=False)
    # 此时已无持仓
    entry = _finalize_position_action(
        "002050.SZ", {"action": "hold", "rationale": "x", "scale_plan": []},
        "text", [], {}, {}, save=True,
    )
    assert entry["source"] == POSITION_ACTION_SOURCE
    # journal 仍写
    assert len(journal.load_position_actions("002050.SZ")) == 1
    # active 无持仓，plan 无处可写（不抛、不重建）
    assert watchlist.load()["active_positions"] == []


@patch("apex.data.get_name_map")
def test_finalize_inherits_playstyle_from_latest_verdict(mock_names, isolated_paths):
    """OV#3: position_action 路径捕获 playstyle -- 从最近 verdict 继承（股票属性，稳定）。"""
    mock_names.return_value = {}
    _write_verdict_with_playstyle("002050.SZ")  # prior verdict 带 playstyle
    _add()
    entry = _finalize_position_action(
        "002050.SZ", {"action": "hold", "rationale": "x", "scale_plan": []},
        "text", [], {}, {}, save=True,
    )
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
    entry = _finalize_position_action(
        "002050.SZ", {"action": "hold", "rationale": "x", "scale_plan": []},
        "text", [], {}, {}, save=True,
    )
    assert entry["position_action"]["new_stop"] is None
    pos = watchlist.load()["active_positions"][0]
    assert pos["stop_loss"] == 11.0  # 未动
    assert pos["plan"]["last_new_stop"] is None
    assert pos["plan"]["last_stop_before"] == 11.0  # 仍记旧 stop（供 delta 展示，即使未变）
