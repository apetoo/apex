"""Deterministic evidence normalization, deduplication, and direction policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any


POLICY_VERSION = "decision-policy-v1"
_DIMENSIONS = ("technical", "fundamental", "capital", "sentiment")
_DIRECTIONAL = {"看多", "偏多", "偏空", "看空"}


class EvidenceClaimError(ValueError):
    pass


@dataclass(frozen=True)
class DecisionPolicyResult:
    verdict: str
    direction_allowed: bool
    net_hardness: float
    evidence_coverage: float
    dimension_scores: dict[str, float]
    counted_claims: list[dict[str, Any]]
    excluded_claims: list[dict[str, Any]]
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _profile_weights(profile: Any) -> dict[str, float]:
    weights = profile.weights if hasattr(profile, "weights") else profile.get("weights", {})
    normalized = {dimension: float(weights.get(dimension, 0.0)) for dimension in _DIMENSIONS}
    total = sum(normalized.values())
    if total <= 0:
        raise ValueError("stock profile weights must sum to a positive value")
    return {key: value / total for key, value in normalized.items()}


def _parse_day(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _business_days_after(start: date, end: date) -> int:
    if end <= start:
        return 0
    current = start
    count = 0
    while current < end:
        current = date.fromordinal(current.toordinal() + 1)
        if current.weekday() < 5:
            count += 1
    return count


def _exclusion_reason(
    claim: dict[str, Any], ledger_item: dict[str, Any], analysis_day: date | None,
    data_quality: dict[str, Any],
) -> str | None:
    if ledger_item.get("evidence_type") == "classification":
        return "classification_not_directional"
    if (
        not data_quality.get("sector_available", True)
        and str(claim.get("independence_group") or "").startswith("sector")
    ):
        return "sector_unavailable"
    if not bool(claim.get("is_complete", True)):
        return "incomplete"
    freshness = str(ledger_item.get("freshness_status") or "current")
    if freshness not in {"current", "fresh"}:
        return f"freshness_{freshness}"
    as_of = _parse_day(claim.get("as_of"))
    dimension = str(claim.get("dimension") or "")
    if analysis_day and as_of:
        if dimension == "sentiment" and as_of != analysis_day:
            return "stale_sentiment"
        if dimension == "capital" and _business_days_after(as_of, analysis_day) > 3:
            return "stale_capital"
        if dimension == "technical" and claim.get("frequency") == "daily" and _business_days_after(as_of, analysis_day) > 1:
            return "stale_technical"
    if int(ledger_item.get("source_tier") or 3) >= 3:
        return "tier3_not_directional"
    return None


def _normalize_group(claim: dict[str, Any]) -> str:
    group = str(claim.get("independence_group") or claim.get("evidence_id") or "")
    if claim.get("dimension") == "sentiment" and group.startswith("market-"):
        return f"market_sentiment:{str(claim.get('as_of') or '')[:10]}"
    return group


def _verdict(net_hardness: float, coverage: float) -> str:
    if coverage < 0.60:
        return "中性"
    if net_hardness >= 2.0:
        return "看多"
    if net_hardness >= 1.0:
        return "偏多"
    if net_hardness > 0.05:
        return "观望偏多"
    if net_hardness <= -2.0:
        return "看空"
    if net_hardness <= -1.0:
        return "偏空"
    if net_hardness < -0.05:
        return "观望偏空"
    return "中性"


def evaluate_decision(
    profile: Any,
    evidence_claims: list[dict[str, Any]],
    data_quality: dict[str, Any],
) -> DecisionPolicyResult:
    """Evaluate a model proposal against the authoritative evidence ledger."""
    weights = _profile_weights(profile)
    ledger = data_quality.get("evidence_ledger") or {}
    analysis_day = _parse_day(data_quality.get("analysis_date"))
    accepted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    stance_counts = {
        stance: sum(1 for claim in evidence_claims if claim.get("stance") == stance)
        for stance in ("bull", "bear")
    }
    if any(count > 3 for count in stance_counts.values()):
        raise EvidenceClaimError("each stance may contain at most 3 evidence claims")

    for raw in evidence_claims:
        claim = dict(raw)
        evidence_id = str(claim.get("evidence_id") or "")
        ledger_item = ledger.get(evidence_id)
        if not ledger_item:
            raise EvidenceClaimError(f"evidence reference missing: {evidence_id or '<empty>'}")
        if not ledger_item.get("entity_matched", True):
            raise EvidenceClaimError(f"evidence entity mismatch: {evidence_id}")
        authoritative_fields = (
            "dimension", "nature", "as_of", "frequency", "is_complete", "independence_group",
        )
        for field in authoritative_fields:
            if field not in ledger_item:
                raise EvidenceClaimError(f"evidence metadata missing: {evidence_id}.{field}")
            if claim.get(field) != ledger_item.get(field):
                raise EvidenceClaimError(f"evidence metadata mismatch: {evidence_id}.{field}")
        claim.update({field: ledger_item[field] for field in authoritative_fields})
        stance = str(claim.get("stance") or "")
        dimension = str(claim.get("dimension") or "")
        nature = str(claim.get("nature") or "")
        if stance not in {"bull", "bear"}:
            raise EvidenceClaimError(f"invalid stance: {stance}")
        if dimension not in _DIMENSIONS:
            raise EvidenceClaimError(f"invalid dimension: {dimension}")
        if nature not in {"fact", "current", "forecast"}:
            raise EvidenceClaimError(f"invalid nature: {nature}")
        try:
            proposed = float(claim.get("hardness"))
        except (TypeError, ValueError):
            raise EvidenceClaimError(f"invalid hardness: {claim.get('hardness')}") from None
        if not 1 <= proposed <= 5:
            raise EvidenceClaimError(f"hardness out of range: {proposed}")

        reason = _exclusion_reason(claim, ledger_item, analysis_day, data_quality)
        if reason:
            excluded.append({**claim, "reason": reason})
            continue
        tier = int(ledger_item.get("source_tier") or 3)
        cap = 5.0 if tier <= 1 else 4.0 if tier == 2 else 2.0
        adjusted = min(proposed, cap)
        if nature == "forecast":
            adjusted = max(1.0, adjusted - 0.5)
        accepted.append({
            **claim,
            "source_tier": tier,
            "adjusted_hardness": adjusted,
            "independence_group": _normalize_group(claim),
        })

    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for claim in accepted:
        key = (claim["independence_group"], claim["stance"])
        previous = deduped.get(key)
        if previous is None or claim["adjusted_hardness"] > previous["adjusted_hardness"]:
            if previous is not None:
                excluded.append({**previous, "reason": "duplicate_independence_group"})
            deduped[key] = claim
        else:
            excluded.append({**claim, "reason": "duplicate_independence_group"})
    counted = list(deduped.values())

    dimension_scores: dict[str, float] = {}
    covered_dimensions: set[str] = set()
    for dimension in _DIMENSIONS:
        rows = [item for item in counted if item["dimension"] == dimension]
        bull = max((item["adjusted_hardness"] for item in rows if item["stance"] == "bull"), default=0.0)
        bear = max((item["adjusted_hardness"] for item in rows if item["stance"] == "bear"), default=0.0)
        dimension_scores[dimension] = bull - bear
        if rows:
            covered_dimensions.add(dimension)
    coverage = sum(weights[dimension] for dimension in covered_dimensions)
    net = sum(weights[dimension] * dimension_scores[dimension] for dimension in _DIMENSIONS)
    coverage = round(coverage, 6)
    net = round(net, 6)
    verdict = _verdict(net, coverage)
    return DecisionPolicyResult(
        verdict=verdict,
        direction_allowed=verdict in _DIRECTIONAL,
        net_hardness=net,
        evidence_coverage=coverage,
        dimension_scores=dimension_scores,
        counted_claims=sorted(counted, key=lambda item: (item["dimension"], item["stance"], item["evidence_id"])),
        excluded_claims=excluded,
    )


def _confidence_cap(result: DecisionPolicyResult) -> int:
    if not result.counted_claims:
        return 1
    evidence_quality = sum(
        float(item["adjusted_hardness"]) / 5.0 for item in result.counted_claims
    ) / len(result.counted_claims)
    return max(1, min(10, round(10.0 * (0.60 * result.evidence_coverage + 0.40 * evidence_quality))))


def apply_accuracy_policy(
    candidate: dict[str, Any],
    profile: Any,
    data_quality: dict[str, Any],
    *,
    forecast_rows: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Make the backend result authoritative while preserving model values for audit."""
    from apex import forecast_calibration

    final = dict(candidate)
    claims = list(final.get("evidence_claims") or [])
    result = evaluate_decision(profile, claims, data_quality)
    model_verdict = str(final.get("verdict") or "中性")
    model_confidence = int(final.get("model_confidence") or final.get("confidence") or 1)
    capped_confidence = min(max(1, min(10, model_confidence)), _confidence_cap(result))
    calibration = forecast_calibration.calibrate_confidence(
        capped_confidence, result.verdict, list(forecast_rows or []),
    )
    ledger = data_quality.get("evidence_ledger") or {}
    final["evidence"] = [
        " → ".join(filter(None, (
            str((ledger.get(item["evidence_id"]) or {}).get("fact") or "").strip(),
            str(item.get("inference") or "").strip(),
        )))
        for item in result.counted_claims
    ]

    final.update({
        "model_verdict": model_verdict,
        "model_confidence": model_confidence,
        "verdict": result.verdict,
        "confidence": int(round(calibration.score)),
        "calibrated_confidence": calibration.score,
        "calibration_sample_size": calibration.sample_size,
        "calibration_applied": calibration.applied,
        "calibration_explanation": calibration.explanation,
        "evidence_coverage": result.evidence_coverage,
        "net_hardness": result.net_hardness,
        "counted_evidence_ids": [item["evidence_id"] for item in result.counted_claims],
    })
    if not result.direction_allowed:
        final["proposed_trade_action"] = "watch"
        final["position_size_pct"] = 0
        for field in ("entry", "entry_low", "entry_high", "stop_loss", "target"):
            final[field] = 0
    elif result.verdict in {"偏空", "看空"}:
        final["proposed_trade_action"] = "avoid"
        final["position_size_pct"] = 0
        for field in ("entry", "entry_low", "entry_high", "stop_loss", "target"):
            final[field] = 0

    metadata = {
        "decision_policy": result.to_dict(),
        "model_verdict": model_verdict,
        "model_confidence": model_confidence,
        "calibration": {
            "score": calibration.score,
            "sample_size": calibration.sample_size,
            "applied": calibration.applied,
            "explanation": calibration.explanation,
        },
    }
    return final, metadata
