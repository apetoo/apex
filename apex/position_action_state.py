"""Materialize position-action proposals against the current position state.

This module deliberately has no persistence or framework dependencies.  It is
the boundary between an action proposal and the complete state consumed by
later review/finalization steps.
"""

from copy import deepcopy
from typing import Literal, TypedDict


LadderIntent = Literal["preserve", "replace", "clear"]


class EffectivePositionPlan(TypedDict, total=False):
    action: str
    add_shares: int | None
    trim_shares: int | None
    trim_pct: float | None
    effective_stop: float | None
    effective_target: float | None
    effective_scale_plan: list[dict]
    ladder_intent: LadderIntent
    rationale: str
    change_summary: dict[str, str]


def adapt_legacy_proposal(raw: dict) -> dict:
    """Add explicit ladder intent to a legacy action proposal."""

    proposal = deepcopy(raw or {})
    if "ladder_intent" not in proposal:
        proposal["ladder_intent"] = "replace" if proposal.get("scale_plan") else "preserve"
        if proposal["ladder_intent"] == "preserve":
            proposal.pop("scale_plan", None)
    return proposal


def validate_proposal_shape(proposal: dict) -> list[str]:
    """Return user-facing issues for ambiguous ladder intent combinations."""

    intent = proposal.get("ladder_intent")
    if intent not in ("preserve", "replace", "clear"):
        return ["ladder_intent 必须是 preserve、replace 或 clear。"]

    if intent == "preserve" and "scale_plan" in proposal:
        return ["ladder_intent=preserve 时不得提交 scale_plan 档位。"]

    if intent == "replace":
        scale_plan = proposal.get("scale_plan")
        if not isinstance(scale_plan, list) or not scale_plan:
            return ["ladder_intent=replace 时 scale_plan 必须是非空完整计划；清空请使用 clear。"]

    if intent == "clear" and "scale_plan" in proposal:
        return ["ladder_intent=clear 时不得提交 scale_plan 档位。"]

    return []


def materialize_effective_position_plan(
    baseline: dict, proposal: dict
) -> EffectivePositionPlan:
    """Combine a proposal with baseline values into a complete effective plan."""

    baseline_copy = deepcopy(baseline or {})
    adapted = adapt_legacy_proposal(proposal)
    issues = validate_proposal_shape(adapted)
    if issues:
        raise ValueError("；".join(issues))

    intent = adapted["ladder_intent"]
    if adapted.get("action") == "exit" or intent == "clear":
        effective_scale_plan = []
        ladder_change = "cleared"
    elif intent == "replace":
        effective_scale_plan = deepcopy(adapted["scale_plan"])
        ladder_change = "replaced"
    else:
        effective_scale_plan = deepcopy(
            baseline_copy.get("plan", {}).get("scale_plan", [])
        )
        ladder_change = "preserved"

    result: EffectivePositionPlan = deepcopy(adapted)
    stop_replaced = "new_stop" in adapted
    target_replaced = "new_target" in adapted
    result.update(
        effective_stop=deepcopy(
            adapted["new_stop"] if stop_replaced else baseline_copy.get("stop_loss")
        ),
        effective_target=deepcopy(
            adapted["new_target"] if target_replaced else baseline_copy.get("target")
        ),
        effective_scale_plan=effective_scale_plan,
        ladder_intent=intent,
        change_summary={
            "stop": "replaced" if stop_replaced else "preserved",
            "target": "replaced" if target_replaced else "preserved",
            "scale_plan": ladder_change,
        },
    )
    return result
