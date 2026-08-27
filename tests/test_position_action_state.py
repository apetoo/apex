"""Tests for the pure position-action effective-state materializer."""

import pytest

from apex.position_action_state import (
    adapt_legacy_proposal,
    materialize_effective_position_plan,
    validate_proposal_shape,
)


BASELINE = {
    "stop_loss": 71.5,
    "target": 90.0,
    "plan": {"scale_plan": [
        {"level": 1, "action": "trim", "trigger_price": 71.5, "pct": 1.0},
        {"level": 2, "action": "add", "trigger_price": 79.0, "shares": 100,
         "new_stop": 73.0},
    ]},
}


def test_preserve_materializes_complete_effective_state():
    proposal = {"action": "hold", "ladder_intent": "preserve", "rationale": "维持"}
    result = materialize_effective_position_plan(BASELINE, proposal)
    assert result["effective_stop"] == 71.5
    assert result["effective_target"] == 90.0
    assert result["effective_scale_plan"] == BASELINE["plan"]["scale_plan"]
    assert result["ladder_intent"] == "preserve"
    assert result["change_summary"] == {
        "stop": "preserved", "target": "preserved", "scale_plan": "preserved",
    }


def test_replace_and_clear_are_explicit():
    replacement = [{"action": "trim", "trigger_price": 75.0, "pct": 0.5}]
    replaced = materialize_effective_position_plan(BASELINE, {
        "action": "hold", "ladder_intent": "replace", "scale_plan": replacement,
    })
    cleared = materialize_effective_position_plan(BASELINE, {
        "action": "hold", "ladder_intent": "clear",
    })
    assert replaced["effective_scale_plan"] == replacement
    assert replaced["change_summary"]["scale_plan"] == "replaced"
    assert cleared["effective_scale_plan"] == []
    assert cleared["change_summary"]["scale_plan"] == "cleared"


def test_legacy_empty_plan_is_safe_preserve_and_nonempty_is_replace():
    assert adapt_legacy_proposal({"action": "hold", "scale_plan": []})["ladder_intent"] == "preserve"
    assert "scale_plan" not in adapt_legacy_proposal({"action": "hold", "scale_plan": []})
    assert adapt_legacy_proposal({
        "action": "hold", "scale_plan": [{"action": "add", "trigger_price": 79, "shares": 100}],
    })["ladder_intent"] == "replace"


def test_intent_shape_rejects_ambiguous_combinations():
    assert validate_proposal_shape({
        "action": "hold", "ladder_intent": "preserve", "scale_plan": [{"level": 1}],
    }) == ["ladder_intent=preserve 时不得提交 scale_plan 档位。"]
    assert validate_proposal_shape({
        "action": "hold", "ladder_intent": "replace", "scale_plan": [],
    }) == ["ladder_intent=replace 时 scale_plan 必须是非空完整计划；清空请使用 clear。"]


def test_new_stop_and_target_replace_baseline_values():
    result = materialize_effective_position_plan(BASELINE, {
        "action": "hold", "ladder_intent": "preserve", "new_stop": 73.0, "new_target": 95.0,
    })
    assert result["effective_stop"] == 73.0
    assert result["effective_target"] == 95.0
    assert result["change_summary"]["stop"] == "replaced"
    assert result["change_summary"]["target"] == "replaced"


def test_materialization_deep_copies_baseline_and_proposal():
    proposal = {"action": "hold", "ladder_intent": "replace",
                "scale_plan": [{"action": "add", "trigger_price": 80.0}]}
    result = materialize_effective_position_plan(BASELINE, proposal)
    result["effective_scale_plan"][0]["trigger_price"] = 81.0
    result["effective_scale_plan"].append({"action": "trim"})
    assert BASELINE["plan"]["scale_plan"][0]["trigger_price"] == 71.5
    assert proposal["scale_plan"] == [{"action": "add", "trigger_price": 80.0}]


def test_exit_clears_effective_ladder_even_when_intent_preserves():
    result = materialize_effective_position_plan(BASELINE, {
        "action": "exit", "ladder_intent": "preserve",
    })
    assert result["effective_scale_plan"] == []
    assert result["change_summary"]["scale_plan"] == "cleared"


def test_invalid_intent_is_reported_and_materialization_raises():
    proposal = {"action": "hold", "ladder_intent": "modify", "scale_plan": []}
    issues = validate_proposal_shape(proposal)
    assert issues == ["ladder_intent 必须是 preserve、replace 或 clear。"]
    with pytest.raises(ValueError, match="ladder_intent 必须是 preserve、replace 或 clear。"):
        materialize_effective_position_plan(BASELINE, proposal)
