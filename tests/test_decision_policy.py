import pytest

from apex.decision_policy import EvidenceClaimError, apply_accuracy_policy, evaluate_decision


def _profile(weights=None):
    return {
        "weights": weights or {
            "technical": 0.30,
            "fundamental": 0.30,
            "capital": 0.20,
            "sentiment": 0.20,
        },
    }


def _ledger(*ids, tier=1):
    dimensions = {
        "tech": "technical", "forecast": "technical", "intraday": "technical", "rumor": "technical",
        "fund": "fundamental", "profile": "fundamental",
        "capital": "capital",
        "sentiment": "sentiment", "regime": "sentiment", "breadth": "sentiment",
        "momentum": "sentiment", "sector": "sentiment",
    }
    return {
        evidence_id: {
            "id": evidence_id,
            "source_tier": tier,
            "entity_matched": True,
            "freshness_status": "current",
            "dimension": next((value for prefix, value in dimensions.items() if evidence_id.startswith(prefix)), "technical"),
            "nature": "forecast" if evidence_id == "forecast" else "current",
            "as_of": "2026-08-31",
            "frequency": "daily",
            "is_complete": True,
            "independence_group": evidence_id,
        }
        for evidence_id in ids
    }


def _claim(evidence_id, dimension, hardness, *, stance="bull", nature="current", **extra):
    return {
        "evidence_id": evidence_id,
        "stance": stance,
        "dimension": dimension,
        "nature": nature,
        "hardness": hardness,
        "as_of": "2026-08-31",
        "frequency": "daily",
        "is_complete": True,
        "independence_group": evidence_id,
        "inference": "测试推论",
        **extra,
    }


def _ledger_for_claims(claims, tier=1):
    ledger = _ledger(*(claim["evidence_id"] for claim in claims), tier=tier)
    for claim in claims:
        ledger[claim["evidence_id"]].update({
            field: claim[field] for field in (
                "dimension", "nature", "as_of", "frequency", "is_complete", "independence_group",
            )
        })
    return ledger


def test_coverage_below_sixty_percent_forces_neutral():
    profile = _profile({"technical": 0.30, "fundamental": 0.29, "capital": 0.21, "sentiment": 0.20})
    claims = [
        _claim("tech", "technical", 5),
        _claim("fund", "fundamental", 5),
    ]

    result = evaluate_decision(profile, claims, {
        "analysis_date": "2026-08-31",
        "evidence_ledger": _ledger("tech", "fund"),
    })

    assert result.evidence_coverage == pytest.approx(0.59)
    assert result.verdict == "中性"
    assert result.direction_allowed is False


def test_sixty_percent_coverage_and_point_nine_is_watch_only():
    claim = _claim("forecast", "technical", 2, nature="forecast")
    result = evaluate_decision(_profile({
        "technical": 0.60, "fundamental": 0.20, "capital": 0.10, "sentiment": 0.10,
    }), [claim], {
        "analysis_date": "2026-08-31",
        "evidence_ledger": _ledger("forecast"),
    })

    assert result.evidence_coverage == pytest.approx(0.60)
    assert result.net_hardness == pytest.approx(0.90)
    assert result.verdict == "观望偏多"
    assert result.direction_allowed is False


@pytest.mark.parametrize(("hardness", "expected"), [(1, "偏多"), (2, "看多")])
def test_direction_boundaries_are_deterministic(hardness, expected):
    claims = [
        _claim("tech", "technical", hardness),
        _claim("fund", "fundamental", hardness),
        _claim("sentiment", "sentiment", hardness),
    ]
    result = evaluate_decision(_profile({
        "technical": 0.40, "fundamental": 0.30, "capital": 0.0, "sentiment": 0.30,
    }), claims, {
        "analysis_date": "2026-08-31",
        "evidence_ledger": _ledger("tech", "fund", "sentiment"),
    })

    assert result.net_hardness == pytest.approx(float(hardness))
    assert result.verdict == expected
    assert result.direction_allowed is True


def test_market_sentiment_derivatives_count_once():
    claims = [
        _claim("regime", "sentiment", 3, independence_group="market-regime"),
        _claim("breadth", "sentiment", 5, independence_group="market-breadth"),
        _claim("momentum", "sentiment", 4, independence_group="market-momentum"),
    ]
    result = evaluate_decision(_profile({
        "technical": 0.0, "fundamental": 0.0, "capital": 0.0, "sentiment": 1.0,
    }), claims, {
        "analysis_date": "2026-08-31",
        "evidence_ledger": _ledger_for_claims(claims),
    })

    assert len(result.counted_claims) == 1
    assert result.counted_claims[0]["evidence_id"] == "breadth"
    assert result.net_hardness == pytest.approx(5.0)


def test_tier_three_cannot_independently_form_direction():
    claims = [_claim("rumor", "technical", 5)]
    result = evaluate_decision(_profile({
        "technical": 1.0, "fundamental": 0.0, "capital": 0.0, "sentiment": 0.0,
    }), claims, {
        "analysis_date": "2026-08-31",
        "evidence_ledger": _ledger("rumor", tier=3),
    })

    assert result.evidence_coverage == 0
    assert result.verdict == "中性"
    assert result.excluded_claims[0]["reason"] == "tier3_not_directional"


def test_unknown_evidence_reference_is_rejected():
    with pytest.raises(EvidenceClaimError, match="missing"):
        evaluate_decision(_profile(), [_claim("missing", "technical", 4)], {
            "analysis_date": "2026-08-31",
            "evidence_ledger": {},
        })


def test_more_than_three_claims_on_one_side_is_rejected():
    claims = [_claim(f"tech-{index}", "technical", 3) for index in range(4)]
    with pytest.raises(EvidenceClaimError, match="at most 3"):
        evaluate_decision(_profile(), claims, {
            "analysis_date": "2026-08-31",
            "evidence_ledger": _ledger(*(item["evidence_id"] for item in claims)),
        })


def test_classification_and_unavailable_sector_evidence_do_not_count():
    claims = [
        _claim("profile", "fundamental", 5),
        _claim("sector", "sentiment", 5, independence_group="sector-relative-strength"),
    ]
    ledger = _ledger_for_claims(claims)
    ledger["profile"]["evidence_type"] = "classification"

    result = evaluate_decision(_profile(), claims, {
        "analysis_date": "2026-08-31",
        "sector_available": False,
        "evidence_ledger": ledger,
    })

    assert result.evidence_coverage == 0
    assert {item["reason"] for item in result.excluded_claims} == {
        "classification_not_directional", "sector_unavailable",
    }


@pytest.mark.parametrize(("field", "lie"), [
    ("dimension", "sentiment"),
    ("nature", "fact"),
    ("as_of", "2026-08-31"),
    ("frequency", "daily"),
    ("is_complete", True),
    ("independence_group", "fake-independent-group"),
])
def test_claim_cannot_override_authoritative_ledger_metadata(field, lie):
    claim = _claim(
        "intraday", "technical", 5, nature="current", as_of="2026-08-30",
        frequency="intraday", is_complete=False, independence_group="same-source",
    )
    claim[field] = lie
    ledger = _ledger("intraday")
    ledger["intraday"].update({
        "dimension": "technical", "nature": "current", "as_of": "2026-08-30",
        "frequency": "intraday", "is_complete": False,
        "independence_group": "same-source",
    })

    with pytest.raises(EvidenceClaimError, match=field):
        evaluate_decision(_profile(), [claim], {
            "analysis_date": "2026-08-31", "evidence_ledger": ledger,
        })


def test_incomplete_intraday_and_stale_capital_do_not_count_for_coverage():
    claims = [
        _claim("intraday", "technical", 5, frequency="intraday", is_complete=False),
        _claim("capital", "capital", 5, as_of="2026-08-25"),
        _claim("fund", "fundamental", 3, frequency="quarterly", as_of="2026-06-30"),
    ]
    result = evaluate_decision(_profile(), claims, {
        "analysis_date": "2026-08-31",
        "evidence_ledger": _ledger_for_claims(claims),
    })

    assert result.evidence_coverage == pytest.approx(0.30)
    assert {item["reason"] for item in result.excluded_claims} == {
        "incomplete", "stale_capital",
    }


def test_accuracy_policy_overrides_model_verdict_and_disables_observation_trade():
    candidate = {
        "verdict": "看多", "confidence": 9, "model_confidence": 9,
        "proposed_trade_action": "buy", "position_size_pct": 40,
        "entry": 10, "entry_low": 9.8, "entry_high": 10.0,
        "stop_loss": 9, "target": 12,
        "evidence_claims": [_claim("forecast", "technical", 2, nature="forecast")],
    }
    final, metadata = apply_accuracy_policy(candidate, _profile({
        "technical": 0.60, "fundamental": 0.20, "capital": 0.10, "sentiment": 0.10,
    }), {
        "analysis_date": "2026-08-31", "evidence_ledger": _ledger("forecast"),
    })

    assert final["model_verdict"] == "看多"
    assert final["verdict"] == "观望偏多"
    assert final["model_confidence"] == 9
    assert final["proposed_trade_action"] == "watch"
    assert final["position_size_pct"] == 0
    assert all(final[field] == 0 for field in ("entry", "stop_loss", "target"))
    assert metadata["decision_policy"]["net_hardness"] == pytest.approx(0.9)


def test_accuracy_policy_uses_evidence_cap_before_forecast_calibration():
    claims = [
        _claim("tech", "technical", 5), _claim("fund", "fundamental", 5),
        _claim("sentiment", "sentiment", 5),
    ]
    final, _ = apply_accuracy_policy({
        "verdict": "看多", "model_confidence": 10, "confidence": 10,
        "proposed_trade_action": "buy", "position_size_pct": 50,
        "evidence_claims": claims,
    }, _profile({
        "technical": 0.40, "fundamental": 0.30, "capital": 0.0, "sentiment": 0.30,
    }), {
        "analysis_date": "2026-08-31",
        "evidence_ledger": _ledger("tech", "fund", "sentiment"),
    })

    assert final["verdict"] == "看多"
    assert final["confidence"] == 10
    assert final["calibration_sample_size"] == 0
