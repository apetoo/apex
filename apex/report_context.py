"""Build the bounded, authoritative input used by detailed reports."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any


MAX_HISTORY = 5
MAX_REASONS = 5
MAX_UNKNOWNS = 8
MAX_TEXT = 240

_FORECAST_OUTCOME_FIELDS = (
    "outcome",
    "matured_at",
    "hit",
    "policy_version",
)


def _text(value: Any) -> str:
    return str(value or "")[:MAX_TEXT]


def _inference_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:MAX_TEXT]


def _pick(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    result = {}
    for key in keys:
        if source.get(key) is None:
            continue
        value = _safe_value(source[key])
        if value is not None:
            result[key] = value
    return result


def _safe_value(value: Any) -> Any:
    """Copy only strict-JSON values, bounding every textual leaf."""
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            str(key)[:MAX_TEXT]: safe
            for key, raw in value.items()
            if (safe := _safe_value(raw)) is not None
        }
    return None


def _scalar(value: Any) -> Any:
    """Return JSON-safe scalar values, dropping containers and exotic objects."""
    return _safe_value(value) if not isinstance(value, dict) else None


def _scalar_fields(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {
        key[:MAX_TEXT]: scalar
        for key, value in source.items()
        if isinstance(key, str) and (scalar := _scalar(value)) is not None
    }


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_timestamp(row: Any, *keys: str) -> datetime | None:
    if not isinstance(row, dict):
        return None
    for key in keys:
        if parsed := _timestamp(row.get(key)):
            return parsed
    return None


def _forecast_key(row: Any) -> tuple[str, datetime] | None:
    if not isinstance(row, dict) or not isinstance(row.get("ts_code"), str):
        return None
    analyzed_at = _row_timestamp(row, "analyzed_at", "date")
    if analyzed_at is None:
        return None
    return row["ts_code"].strip(), analyzed_at


def _forecast_outcome(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    result = {}
    for key in _FORECAST_OUTCOME_FIELDS:
        if source.get(key) is None:
            continue
        if (value := _scalar(source[key])) is not None:
            result[key] = value
    return result


def _bounded_reasons(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_text(item) for item in value[:MAX_REASONS] if item is not None]


def _claim(claim: Any, *, excluded: bool = False) -> dict[str, Any]:
    if not isinstance(claim, dict):
        return {}
    keys = (
        ("evidence_id", "dimension", "nature", "as_of", "frequency")
        if excluded else
        (
            "evidence_id", "stance", "dimension", "nature", "hardness",
            "adjusted_hardness", "as_of", "frequency", "inference",
        )
    )
    result = _pick(claim, keys)
    if "inference" in result:
        result["inference"] = _inference_text(result["inference"])
    if excluded and claim.get("reason") is not None:
        result["reason"] = _text(claim["reason"])
    return result


def build_report_context(
    *,
    history_entries: list[dict],
    market_context: dict,
    playstyle: dict | None,
    playstyle_features: dict,
    playstyle_fit: dict | None,
    risk_level: str | None,
    decision_policy: dict,
    unknowns: list[str],
    forecast_rows: list[dict] | None = None,
) -> dict:
    """Select only bounded, report-authoritative data from analysis state."""
    market = {}
    if isinstance(market_context, dict):
        if isinstance(market_context.get("market_sentiment"), dict):
            sentiment = _pick(
                market_context["market_sentiment"],
                ("regime", "market_style", "total_score"),
            )
            reasons = _bounded_reasons(market_context["market_sentiment"].get("reasons"))
            if reasons:
                sentiment["reasons"] = reasons
            if sentiment:
                market["sentiment"] = sentiment
        if isinstance(market_context.get("stock_relative"), dict):
            relative = _pick(
                market_context["stock_relative"],
                ("chg_5d_pct", "chg_20d_pct", "vs_index_5d_pct", "ref_index_name"),
            )
            if relative:
                market["stock_relative"] = relative
        if isinstance(market_context.get("intraday"), dict):
            intraday = _pick(
                market_context["intraday"],
                ("trade_date", "as_of_time", "is_intraday", "day_chg_pct",
                 "amplitude_pct", "vol_ratio", "vol_label", "shape",
                 "vwap_position_pct"),
            )
            if intraday:
                market["intraday"] = intraday
        if market_context.get("as_of") is not None:
            as_of = _safe_value(market_context["as_of"])
            if as_of is not None:
                market["as_of"] = as_of

    refreshed_by_entry: dict[tuple[str, datetime], tuple[tuple, dict]] = {}
    for index, row in enumerate(forecast_rows or []):
        key = _forecast_key(row)
        if key is None:
            continue
        matured_at = _row_timestamp(row, "matured_at")
        rank = (
            matured_at is not None,
            matured_at or datetime.min.replace(tzinfo=timezone.utc),
            index,
        )
        if key not in refreshed_by_entry or rank > refreshed_by_entry[key][0]:
            refreshed_by_entry[key] = rank, row

    sortable_history = [
        (entry, index, _row_timestamp(entry, "analyzed_at", "date"))
        for index, entry in enumerate(history_entries or [])
        if isinstance(entry, dict)
    ]
    sortable_history.sort(
        key=lambda item: (
            item[2] is not None,
            item[2] or datetime.min.replace(tzinfo=timezone.utc),
            item[1],
        ),
        reverse=True,
    )

    history = []
    for entry, _index, _analyzed_at in sortable_history[:MAX_HISTORY]:
        if not isinstance(entry, dict):
            continue
        item = _pick(entry, (
            "analyzed_at", "date", "verdict", "confidence", "calibrated_confidence",
        ))
        refreshed = refreshed_by_entry.get(_forecast_key(entry))
        outcome = refreshed[1] if refreshed is not None else entry.get("forecast_outcome")
        if safe_outcome := _forecast_outcome(outcome):
            item["forecast_outcome"] = safe_outcome
        history.append(item)

    profile = {}
    if isinstance(playstyle, dict):
        profile = _pick(playstyle, (
            "primary", "secondary", "ratings", "method", "low_confidence",
        ))
        reasons = _bounded_reasons(playstyle.get("reasons"))
        if reasons:
            profile["reasons"] = reasons

    features = {}
    if isinstance(playstyle_features, dict) and isinstance(playstyle_features.get("features"), dict):
        features = {
            name[:MAX_TEXT]: _scalar_fields(group)
            for name, group in playstyle_features["features"].items()
            if isinstance(name, str) and _scalar_fields(group)
        }
    playstyle_context = {}
    if profile:
        playstyle_context["profile"] = profile
    if risk_level is not None:
        playstyle_context["risk_level"] = _text(risk_level)
    if isinstance(playstyle_fit, dict):
        fit = _pick(playstyle_fit, ("state", "note"))
        if "note" in fit:
            fit["note"] = _text(fit["note"])
        if fit:
            playstyle_context["fit"] = fit
    if features:
        playstyle_context["features"] = features

    counted = decision_policy.get("counted_claims", []) if isinstance(decision_policy, dict) else []
    excluded = decision_policy.get("excluded_claims", []) if isinstance(decision_policy, dict) else []
    evidence_selection = {
        "counted": [_claim(claim) for claim in counted if _claim(claim)],
        "excluded": [_claim(claim, excluded=True) for claim in excluded if _claim(claim, excluded=True)],
    }
    safe_unknowns = [_text(item) for item in (unknowns or [])[:MAX_UNKNOWNS] if item is not None]
    return {
        "market": market,
        "history": history,
        "playstyle": playstyle_context,
        "evidence_selection": evidence_selection,
        "unknowns": safe_unknowns,
    }
