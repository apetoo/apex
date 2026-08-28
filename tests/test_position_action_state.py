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
    empty = adapt_legacy_proposal({"action": "hold", "scale_plan": []})
    assert empty["ladder_intent"] == "preserve"
    assert "scale_plan" not in empty
    assert empty["compatibility_warnings"] == [
        "旧版缺失或空 scale_plan 已按 preserve 兼容；如需清空请显式使用 ladder_intent=clear。"
    ]
    missing = adapt_legacy_proposal({"action": "hold"})
    assert missing["compatibility_warnings"] == empty["compatibility_warnings"]
    assert adapt_legacy_proposal({
        "action": "hold", "scale_plan": [{"action": "add", "trigger_price": 79, "shares": 100}],
    })["ladder_intent"] == "replace"
    assert "compatibility_warnings" not in adapt_legacy_proposal({
        "action": "hold", "ladder_intent": "preserve",
    })


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


def test_effective_state_excludes_proposal_only_patch_fields():
    result = materialize_effective_position_plan(BASELINE, {
        "action": "hold", "ladder_intent": "replace",
        "new_stop": 73.0, "new_target": 95.0,
        "scale_plan": [{"action": "trim", "trigger_price": 80.0, "pct": 1.0}],
        "compatibility_warnings": ["legacy"], "rationale": "上移保护位",
    })

    assert result["effective_stop"] == 73.0
    assert result["effective_target"] == 95.0
    assert result["effective_scale_plan"] == [{
        "action": "trim", "trigger_price": 80.0, "pct": 1.0,
    }]
    assert {"new_stop", "new_target", "scale_plan", "compatibility_warnings"}.isdisjoint(result)


def test_explicit_null_stop_and_target_preserve_baseline_values():
    result = materialize_effective_position_plan(BASELINE, {
        "action": "hold", "ladder_intent": "preserve",
        "new_stop": None, "new_target": None,
    })

    assert result["effective_stop"] == 71.5
    assert result["effective_target"] == 90.0
    assert result["change_summary"]["stop"] == "preserved"
    assert result["change_summary"]["target"] == "preserved"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("new_stop", True),
        ("new_stop", float("inf")),
        ("new_stop", "71.5"),
        ("new_stop", 0),
        ("new_stop", -1),
        ("new_stop", float("nan")),
        ("new_target", True),
        ("new_target", "90"),
        ("new_target", 0),
        ("new_target", -1),
        ("new_target", float("nan")),
    ],
)
def test_explicit_stop_and_target_reject_non_positive_or_non_finite_values(field, value):
    issues = validate_proposal_shape({
        "action": "hold", "ladder_intent": "preserve", field: value,
    })

    assert any(field in issue for issue in issues)


def test_materialization_deep_copies_baseline_and_proposal():
    proposal = {"action": "hold", "ladder_intent": "replace",
                "scale_plan": [{"action": "add", "trigger_price": 80.0, "shares": 100}]}
    result = materialize_effective_position_plan(BASELINE, proposal)
    result["effective_scale_plan"][0]["trigger_price"] = 81.0
    result["effective_scale_plan"].append({"action": "trim"})
    assert BASELINE["plan"]["scale_plan"][0]["trigger_price"] == 71.5
    assert proposal["scale_plan"] == [{"action": "add", "trigger_price": 80.0, "shares": 100}]


def test_exit_clears_effective_ladder_even_when_intent_preserves():
    result = materialize_effective_position_plan(BASELINE, {
        "action": "exit", "ladder_intent": "preserve",
    })
    assert result["effective_scale_plan"] == []
    assert result["change_summary"]["scale_plan"] == "cleared"


def test_materializer_normalizes_a_full_exit_ladder_once_for_every_consumer():
    result = materialize_effective_position_plan(BASELINE, {
        "action": "hold", "ladder_intent": "replace",
        "scale_plan": [{
            "action": "trim", "trigger_price": 71.5, "pct": 1.0, "new_stop": 0,
        }],
    })

    assert result["effective_scale_plan"] == [{
        "action": "trim", "trigger_price": 71.5, "pct": 1.0, "new_stop": None,
    }]


def test_replace_validates_every_ladder_level_before_materialization():
    issues = validate_proposal_shape({
        "action": "hold", "ladder_intent": "replace",
        "scale_plan": [
            "not-an-object",
            {"action": "hold", "trigger_price": float("inf"), "shares": 0, "pct": 1.5, "new_stop": 0},
            {"action": "trim", "trigger_price": 71.5, "shares": True},
            {"action": "trim", "trigger_price": 71.5, "pct": 1.0, "new_stop": 0},
        ],
    })

    assert any("第 1 档必须是对象" in issue for issue in issues)
    assert any("第 2 档 action" in issue for issue in issues)
    assert any("第 2 档 trigger_price" in issue for issue in issues)
    assert any("第 2 档 shares/pct" in issue for issue in issues)
    assert any("第 2 档 new_stop" in issue for issue in issues)
    assert any("第 3 档 shares" in issue for issue in issues)
    assert not any("第 4 档 new_stop" in issue for issue in issues)


def test_invalid_intent_is_reported_and_materialization_raises():
    proposal = {"action": "hold", "ladder_intent": "modify", "scale_plan": []}
    issues = validate_proposal_shape(proposal)
    assert issues == ["ladder_intent 必须是 preserve、replace 或 clear。"]
    with pytest.raises(ValueError, match="ladder_intent 必须是 preserve、replace 或 clear。"):
        materialize_effective_position_plan(BASELINE, proposal)
