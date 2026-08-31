import pytest

from apex import skills
from apex.stock_profile import classify_stock_profile


def _borderline_features(circ_mv_yi: float) -> dict:
    return {
        "circ_mv_yi": circ_mv_yi,
        "pe_ttm": 71.6,
        "turnover_rate": 9.56,
        "industry": "汽车零部件",
        "revenue_yoy": 18.0,
        "dragon_tiger_count_20d": 2,
    }


def test_market_cap_boundary_changes_weights_continuously():
    below = classify_stock_profile(_borderline_features(97.52))
    above = classify_stock_profile(_borderline_features(101.0))

    assert below.primary_type in {"题材游资", "成长股"}
    assert above.primary_type in {"题材游资", "成长股"}
    assert {below.primary_type, below.secondary_type} >= {"题材游资", "成长股"}
    assert {above.primary_type, above.secondary_type} >= {"题材游资", "成长股"}
    for dimension in ("technical", "fundamental", "capital", "sentiment"):
        assert abs(below.weights[dimension] - above.weights[dimension]) < 0.03


def test_cyclical_industry_has_priority():
    profile = classify_stock_profile({
        "circ_mv_yi": 620,
        "pe_ttm": 8,
        "turnover_rate": 1.5,
        "industry": "煤炭开采",
        "revenue_yoy": -5,
        "dragon_tiger_count_20d": 0,
    })

    assert profile.primary_type == "周期股"
    assert profile.membership_scores["周期股"] >= 0.9
    assert profile.weights == pytest.approx({
        "technical": 0.25,
        "fundamental": 0.35,
        "capital": 0.20,
        "sentiment": 0.20,
    })


@pytest.mark.parametrize(
    ("growth_score", "expected"),
    [(0.34, "disabled"), (0.35, "mixed"), (0.64, "mixed"), (0.65, "required")],
)
def test_growth_valuation_mode_has_explicit_thresholds(growth_score, expected):
    profile = classify_stock_profile({
        **_borderline_features(150),
        "growth_score_override": growth_score,
    })

    assert profile.growth_valuation_mode == expected


def test_analysis_skills_are_loaded_selectively():
    assert "成长股分析方法论" not in skills.load_skills_for("analyze", names=[])
    assert "成长股分析方法论" in skills.load_skills_for("analyze", names=["growth-stock"])
