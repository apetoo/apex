"""ladder 路径模拟校验：_validate_position_action(current_price=...) 的路径自洽测试。

设计：ladder 的执行语义 = 按价格顺序触发，校验照执行方式模拟——
下行路径（现价往下，trigger 降序）与上行路径（trigger 升序）各走一遍：

- 下行路径必须单一意图（纯 add 或纯 trim）：先卖后买/先买后卖 = churn -> 拒（600989 场景）
- 上行路径允许 add->trim（金字塔加仓后高位兑现）；trim->add = 卖了更高接回 -> 拒
- 路径内 add 的 new_stop 上移止损后，低于新止损的 trim = 路径内死档 -> 拒（含守卫 5 的 trim<止损）
- 上行档 new_stop 杀死下行 trim = 条件死档（仅当上行执行到） -> advisory 不拒（守卫 6 语义，
  一般化：上行 trim 的 new_stop 同样是杀手）
- current_price=None -> 降级为锚定无关检查（守卫 5/6/7 旧行为），不做路径检查
"""
from apex import watchlist
from apex.analyze import _validate_position_action


def _add600989():
    """600989 实盘形状：entry 24.43 / stop 21.0 / target 27.5 / 300 股。"""
    return watchlist.add_position("600989.SH", "宝丰能源", 24.43, 21.0, 27.5,
                                  position_size_shares=300)


# 2026-07-31 实盘 ladder：L1 回踩加 / L2 突破加 / L3 目标减 / L4 破位防守减
_LADDER_600989 = [
    {"level": 1, "action": "add", "trigger_price": 21.8, "shares": 100, "new_stop": 21.0},
    {"level": 2, "action": "add", "trigger_price": 24.5, "shares": 100, "new_stop": 22.5},
    {"level": 3, "action": "trim", "trigger_price": 27.0, "pct": 0.33, "new_stop": 25.0},
    {"level": 4, "action": "trim", "trigger_price": 22.0, "pct": 0.33, "new_stop": 21.0},
]

_PRICE = 23.10  # 7/31 盘中附近


# ── 下行路径单一意图（600989 churn 场景）────────────────────────────────────

def test_down_path_trim_then_add_is_churn_rejected(isolated_paths):
    """600989 7/31 实盘 case：下行路径 trim@22.00 先于 add@21.80 触发 = 先卖后买 churn -> 拒。"""
    _add600989()
    reasons, _ = _validate_position_action(
        {"action": "hold", "new_stop": 21.0, "rationale": "x", "scale_plan": _LADDER_600989},
        "600989.SH", [], current_price=_PRICE,
    )
    assert reasons
    assert any("churn" in r or "先卖后买" in r for r in reasons)


def test_down_path_add_then_trim_is_churn_rejected(isolated_paths):
    """下行路径先买后卖（add@22.0 后 trim@21.5）同样是 churn -> 拒。"""
    _add600989()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x", "scale_plan": [
            {"level": 1, "action": "add", "trigger_price": 22.0, "shares": 100},
            {"level": 2, "action": "trim", "trigger_price": 21.5, "pct": 0.33},
        ]},
        "600989.SH", [], current_price=_PRICE,
    )
    assert reasons
    assert any("churn" in r or "先买后卖" in r for r in reasons)


def test_down_path_trim_only_staged_exit_ok(isolated_paths):
    """纯 trim 下行路径（分批撤退 23.0 -> 22.0）自洽 -> 过。"""
    _add600989()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x", "scale_plan": [
            {"level": 1, "action": "trim", "trigger_price": 23.0, "pct": 0.33},
            {"level": 2, "action": "trim", "trigger_price": 22.0, "pct": 0.33},
        ]},
        "600989.SH", [], current_price=_PRICE,
    )
    assert reasons == []


def test_down_path_add_only_ok(isolated_paths):
    """纯 add 下行路径（回踩分批加 22.5 -> 22.0）自洽 -> 过。"""
    _add600989()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x", "scale_plan": [
            {"level": 1, "action": "add", "trigger_price": 22.5, "shares": 100},
            {"level": 2, "action": "add", "trigger_price": 22.0, "shares": 100},
        ]},
        "600989.SH", [], current_price=_PRICE,
    )
    assert reasons == []


# ── 上行路径：金字塔（add->trim）合法，trim->add 是 churn ─────────────────────

def test_up_path_add_then_trim_pyramid_ok(isolated_paths):
    """上行 add@24.5 -> trim@27.0（金字塔加仓后高位兑现）-> 过。"""
    _add600989()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x", "scale_plan": [
            {"level": 1, "action": "add", "trigger_price": 24.5, "shares": 100, "new_stop": 22.5},
            {"level": 2, "action": "trim", "trigger_price": 27.0, "pct": 0.33, "new_stop": 25.0},
        ]},
        "600989.SH", [], current_price=_PRICE,
    )
    assert reasons == []


def test_up_path_trim_then_add_is_churn_rejected(isolated_paths):
    """上行 trim@24.5 后 add@26.0 更高价接回 = churn -> 拒。"""
    _add600989()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x", "scale_plan": [
            {"level": 1, "action": "trim", "trigger_price": 24.5, "pct": 0.33},
            {"level": 2, "action": "add", "trigger_price": 26.0, "shares": 100},
        ]},
        "600989.SH", [], current_price=_PRICE,
    )
    assert reasons
    assert any("churn" in r or "接回" in r for r in reasons)


# ── 路径内死档（含守卫 5 等价）──────────────────────────────────────────────

def test_in_path_dead_trim_after_stop_raise_rejected(isolated_paths):
    """下行 add@22.0(new_stop 21.5) 执行后 trim@21.2 低于新止损 = 路径内死档 -> 拒。"""
    _add600989()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x", "scale_plan": [
            {"level": 1, "action": "add", "trigger_price": 22.0, "shares": 100, "new_stop": 21.5},
            {"level": 2, "action": "trim", "trigger_price": 21.2, "pct": 0.33},
        ]},
        "600989.SH", [], current_price=_PRICE,
    )
    assert reasons
    assert any("死档" in r or "死代码" in r for r in reasons)


def test_trim_below_initial_stop_rejected_in_path_mode(isolated_paths):
    """路径模式下 trim@20.5 < 初始止损 21.0 -> 拒（守卫 5 的路径形态）。"""
    _add600989()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x", "scale_plan": [
            {"level": 1, "action": "trim", "trigger_price": 20.5, "pct": 0.33},
        ]},
        "600989.SH", [], current_price=_PRICE,
    )
    assert reasons
    assert any("止损" in r for r in reasons)


# ── 跨路径条件死档（守卫 6 语义，advisory 不拒）─────────────────────────────

def test_cross_path_add_new_stop_kills_down_trim_advisory(isolated_paths):
    """上行 add@24.5(new_stop 22.5) 若执行，下行 trim@22.0 永不触发 -> advisory 不拒。"""
    _add600989()
    reasons, warn = _validate_position_action(
        {"action": "hold", "rationale": "x", "scale_plan": [
            {"level": 1, "action": "add", "trigger_price": 24.5, "shares": 100, "new_stop": 22.5},
            {"level": 2, "action": "trim", "trigger_price": 22.0, "pct": 0.33},
        ]},
        "600989.SH", [], current_price=_PRICE,
    )
    assert reasons == []
    assert "跨档自洽" in warn
    assert "trim@22" in warn


def test_cross_path_up_trim_new_stop_kills_down_trim_advisory(isolated_paths):
    """一般化：上行 trim@27.0(new_stop 25.0) 若执行，下行 trim@22.0 同样永不触发 -> advisory。"""
    _add600989()
    reasons, warn = _validate_position_action(
        {"action": "hold", "rationale": "x", "scale_plan": [
            {"level": 1, "action": "trim", "trigger_price": 27.0, "pct": 0.33, "new_stop": 25.0},
            {"level": 2, "action": "trim", "trigger_price": 22.0, "pct": 0.33},
        ]},
        "600989.SH", [], current_price=_PRICE,
    )
    assert reasons == []
    assert "跨档自洽" in warn


# ── 上行 add ≥ 止盈（守卫 7 的路径形态）──────────────────────────────────────

def test_add_above_target_rejected_in_path_mode(isolated_paths):
    """add@28.0 ≥ target 27.5 -> 拒，提示 new_target 修复路径。"""
    _add600989()
    reasons, _ = _validate_position_action(
        {"action": "hold", "rationale": "x", "scale_plan": [
            {"level": 1, "action": "add", "trigger_price": 28.0, "shares": 100},
        ]},
        "600989.SH", [], current_price=_PRICE,
    )
    assert reasons
    assert any("止盈" in r and "new_target" in r for r in reasons)


# ── 降级模式：无 current_price 退化为锚定无关检查，不做路径检查 ──────────────

def test_no_price_degrades_churn_undetectable(isolated_paths):
    """无现价 -> 600989 churn ladder 通过（路径检查跳过），文档化降级行为。"""
    _add600989()
    reasons, _ = _validate_position_action(
        {"action": "hold", "new_stop": 21.0, "rationale": "x", "scale_plan": _LADDER_600989},
        "600989.SH", [],
    )
    assert reasons == []
