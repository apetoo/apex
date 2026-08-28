"""Materialize position-action proposals against the current position state.

This module deliberately has no persistence or framework dependencies.  It is
the boundary between an action proposal and the complete state consumed by
later review/finalization steps.
"""

from copy import deepcopy
import math
from typing import Any, Literal, TypedDict


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


_LEGACY_PRESERVE_WARNING = (
    "旧版缺失或空 scale_plan 已按 preserve 兼容；如需清空请显式使用 ladder_intent=clear。"
)


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_positive_finite_number(value: Any) -> bool:
    return _is_finite_number(value) and float(value) > 0


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_canonical_full_exit(level: dict) -> bool:
    """Return whether a level's nonpositive stop is a full-exit sentinel."""

    return (
        level.get("action") == "trim"
        and _is_finite_number(level.get("pct"))
        and float(level["pct"]) == 1.0
        and _is_finite_number(level.get("new_stop"))
        and float(level["new_stop"]) <= 0
    )


def normalize_effective_scale_plan(scale_plan: Any) -> Any:
    """Deep-copy a ladder and canonicalize only the historical full-exit sentinel."""

    if not isinstance(scale_plan, list):
        return deepcopy(scale_plan)
    normalized = deepcopy(scale_plan)
    for level in normalized:
        if isinstance(level, dict) and _is_canonical_full_exit(level):
            level["new_stop"] = None
    return normalized


def position_trading_fingerprint(position: dict | None) -> dict | None:
    """Return exactly the frozen fields that make a position-action still safe to save."""

    if not isinstance(position, dict) or not position:
        return None
    plan = position.get("plan")
    scale_plan = plan.get("scale_plan") if isinstance(plan, dict) else None
    return {
        "position_size_shares": deepcopy(position.get("position_size_shares")),
        "stop_loss": deepcopy(position.get("stop_loss")),
        "target": deepcopy(position.get("target")),
        "scale_plan": normalize_effective_scale_plan(scale_plan),
    }


def adapt_legacy_proposal(raw: dict) -> dict:
    """Add explicit ladder intent to a legacy action proposal."""

    proposal = deepcopy(raw or {})
    if "ladder_intent" not in proposal:
        legacy_preserve = not proposal.get("scale_plan")
        proposal["ladder_intent"] = "preserve" if legacy_preserve else "replace"
        if proposal["ladder_intent"] == "preserve":
            proposal.pop("scale_plan", None)
            proposal["compatibility_warnings"] = [
                *_as_warning_list(proposal.get("compatibility_warnings")),
                _LEGACY_PRESERVE_WARNING,
            ]
    return proposal


def _as_warning_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def validate_proposal_shape(proposal: dict) -> list[str]:
    """Return deterministic pre-review errors for position-action proposal shape."""

    issues: list[str] = []
    intent = proposal.get("ladder_intent")
    if intent not in ("preserve", "replace", "clear"):
        return ["ladder_intent 必须是 preserve、replace 或 clear。"]

    for field in ("new_stop", "new_target"):
        value = proposal.get(field)
        if field in proposal and value is not None and not _is_positive_finite_number(value):
            issues.append(f"{field} 必须是有限且大于 0 的数值；null 表示保持原值。")

    if intent == "preserve" and "scale_plan" in proposal:
        return [*issues, "ladder_intent=preserve 时不得提交 scale_plan 档位。"]

    if intent == "replace":
        scale_plan = proposal.get("scale_plan")
        if not isinstance(scale_plan, list) or not scale_plan:
            return [*issues, "ladder_intent=replace 时 scale_plan 必须是非空完整计划；清空请使用 clear。"]
        for index, level in enumerate(scale_plan, start=1):
            prefix = f"scale_plan 第 {index} 档"
            if not isinstance(level, dict):
                issues.append(f"{prefix}必须是对象。")
                continue
            if level.get("action") not in ("add", "trim"):
                issues.append(f"{prefix} action 必须是 add 或 trim。")
            if not _is_positive_finite_number(level.get("trigger_price")):
                issues.append(f"{prefix} trigger_price 必须是有限且大于 0 的数值。")

            shares = level.get("shares")
            pct = level.get("pct")
            valid_shares = _is_positive_int(shares)
            valid_pct = _is_finite_number(pct) and 0 < float(pct) <= 1
            if "shares" in level and not valid_shares:
                issues.append(f"{prefix} shares 必须是大于 0 的整数。")
            if "pct" in level and not valid_pct:
                issues.append(f"{prefix} pct 必须是有限且位于 (0, 1] 的数值。")
            if valid_shares == valid_pct:
                issues.append(f"{prefix} shares/pct 必须且只能提供一个有效数量。")

            new_stop = level.get("new_stop")
            if (
                "new_stop" in level
                and new_stop is not None
                and not _is_canonical_full_exit(level)
                and not _is_positive_finite_number(new_stop)
            ):
                issues.append(
                    f"{prefix} new_stop 必须是有限且大于 0 的数值；"
                    "仅 trim pct=1.0 的完整退出档可用非正数并会规范化为 null。"
                )

    if intent == "clear" and "scale_plan" in proposal:
        return [*issues, "ladder_intent=clear 时不得提交 scale_plan 档位。"]

    return issues


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
        effective_scale_plan = normalize_effective_scale_plan(adapted["scale_plan"])
        ladder_change = "replaced"
    else:
        effective_scale_plan = normalize_effective_scale_plan(
            baseline_copy.get("plan", {}).get("scale_plan", [])
        )
        ladder_change = "preserved"

    stop_replaced = adapted.get("new_stop") is not None
    target_replaced = adapted.get("new_target") is not None
    result: EffectivePositionPlan = {
        "action": adapted.get("action"),
        "add_shares": deepcopy(adapted.get("add_shares")),
        "trim_shares": deepcopy(adapted.get("trim_shares")),
        "trim_pct": deepcopy(adapted.get("trim_pct")),
        "effective_stop": deepcopy(
            adapted["new_stop"] if stop_replaced else baseline_copy.get("stop_loss")
        ),
        "effective_target": deepcopy(
            adapted["new_target"] if target_replaced else baseline_copy.get("target")
        ),
        "effective_scale_plan": effective_scale_plan,
        "ladder_intent": intent,
        "rationale": deepcopy(adapted.get("rationale")),
        "change_summary": {
            "stop": "replaced" if stop_replaced else "preserved",
            "target": "replaced" if target_replaced else "preserved",
            "scale_plan": ladder_change,
        },
    }
    return result
