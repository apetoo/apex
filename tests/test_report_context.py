from apex.report_context import build_report_context
import json
import math


def _context():
    return build_report_context(
        history_entries=[{
            "analyzed_at": "2026-08-30T10:00:00+08:00",
            "verdict": "偏空", "confidence": 4,
            "calibrated_confidence": 3,
            "forecast_outcome": {"return_pct": 19.2, "hit": False},
            "analysis_text": "研究草稿中的 PEG=0.43 不得进入",
            "evidence": [{"fact": "未确认现金流 -8.65 亿"}],
        }],
        market_context={
            "as_of": "20260831",
            "stock_relative": {"chg_5d_pct": 11.5, "chg_20d_pct": 42.24,
                               "vs_index_5d_pct": 10.14, "ref_index_name": "沪深300"},
            "market_sentiment": {"regime": "亢奋", "market_style": "高潮",
                                 "total_score": 81, "reasons": ["涨停83家/跌停0家"],
                                 "private_raw": "drop"},
            "intraday": {"is_intraday": False, "shape": "高开低走（冲高回落）",
                         "day_chg_pct": -0.06, "vwap_position_pct": 0.67,
                         "raw_bars": [1, 2, 3]},
            "raw_response": "drop",
        },
        playstyle={"primary": "波段", "secondary": "中线", "ratings": {"波段": 4},
                   "reasons": ["20日波动率82.31%"], "method": "ai_skill"},
        playstyle_features={"completeness": 1.0, "risk_level": "high", "features": {
            "volatility": {"vol_20d_pct": 82.31, "present": True},
            "valuation": {"pe_ttm": 39.69, "present": True},
        }, "notes": ["资金日部分缺失"]},
        playstyle_fit={"state": "insufficient_data", "note": "画像累积中"},
        risk_level="high",
        decision_policy={
            "counted_claims": [{"evidence_id": "sys_capital", "dimension": "capital",
                                "stance": "bear", "inference": "近5日资金流出"}],
            "excluded_claims": [{"evidence_id": "ev_old", "reason": "stale_capital",
                                 "inference": "旧龙虎榜资金"}],
        },
        unknowns=["现金流口径未确认"],
    )


def test_build_report_context_keeps_only_whitelisted_authority():
    context = _context()
    rendered = repr(context)
    assert context["market"]["sentiment"]["regime"] == "亢奋"
    assert context["history"][0]["verdict"] == "偏空"
    assert context["playstyle"]["profile"]["primary"] == "波段"
    assert context["evidence_selection"]["excluded"][0]["reason"] == "stale_capital"
    assert context["unknowns"] == ["现金流口径未确认"]
    assert "analysis_text" not in rendered
    assert "PEG=0.43" not in rendered
    assert "未确认现金流" not in rendered
    assert "raw_bars" not in rendered
    assert "raw_response" not in rendered
    assert "private_raw" not in rendered


def test_build_report_context_returns_explicit_empty_states():
    context = build_report_context(
        history_entries=[], market_context={}, playstyle=None, playstyle_features={},
        playstyle_fit=None, risk_level=None, decision_policy={}, unknowns=[],
    )
    assert context == {
        "market": {}, "history": [], "playstyle": {},
        "evidence_selection": {"counted": [], "excluded": []}, "unknowns": [],
    }


def test_build_report_context_sanitizes_allowed_values_and_bounds_text():
    class Unserializable:
        def __str__(self):
            return "untrusted"

    context = build_report_context(
        history_entries=[{"verdict": Unserializable(), "analyzed_at": "x" * 500}],
        market_context={
            "as_of": "a" * 500,
            "market_sentiment": {"regime": "r" * 500, "total_score": math.inf},
            "stock_relative": {"ref_index_name": Unserializable(), "chg_5d_pct": math.nan},
        },
        playstyle={"primary": "p" * 500, "ratings": {"波段": Unserializable()}},
        playstyle_features={"features": {"volatility": {"value": math.inf, "label": "v" * 500}}},
        playstyle_fit={"state": "s" * 500},
        risk_level="risk" * 100,
        decision_policy={"counted_claims": [{"evidence_id": Unserializable(), "inference": "i" * 500}]},
        unknowns=["u" * 500],
    )
    json.dumps(context, allow_nan=False)
    assert context["history"][0]["analyzed_at"] == "x" * 240
    assert "verdict" not in context["history"][0]
    assert context["market"]["sentiment"]["regime"] == "r" * 240
    assert "total_score" not in context["market"]["sentiment"]
    assert "ref_index_name" not in context["market"].get("stock_relative", {})
    assert "chg_5d_pct" not in context["market"].get("stock_relative", {})
    assert context["playstyle"]["profile"]["primary"] == "p" * 240
    assert context["playstyle"]["profile"]["ratings"] == {}
    assert context["playstyle"]["features"]["volatility"]["label"] == "v" * 240
    assert "value" not in context["playstyle"]["features"]["volatility"]
    assert context["evidence_selection"]["counted"][0]["inference"] == "i" * 240
    assert "evidence_id" not in context["evidence_selection"]["counted"][0]
    assert context["unknowns"] == ["u" * 240]


def test_build_report_context_normalizes_inference_to_bounded_single_line():
    context = build_report_context(
        history_entries=[], market_context={}, playstyle=None,
        playstyle_features={}, playstyle_fit=None, risk_level=None,
        decision_policy={
            "counted_claims": [{
                "evidence_id": "sys_capital",
                "inference": "近 5 日资金 \r\n\t净流出" + " x" * 300,
            }],
            "excluded_claims": [],
        },
        unknowns=[],
    )

    inference = context["evidence_selection"]["counted"][0]["inference"]
    assert inference.startswith("近 5 日资金 净流出 x")
    assert "\n" not in inference
    assert "\r" not in inference
    assert "\t" not in inference
    assert len(inference) <= 240


def test_build_report_context_sanitizes_market_and_feature_keys():
    class Unserializable:
        def __str__(self):
            return "unsafe-key"

    long_group = "g" * 500
    context = build_report_context(
        history_entries=[],
        market_context={"as_of": Unserializable()},
        playstyle=None,
        playstyle_features={"features": {
            long_group: {Unserializable(): 1, "f" * 500: "ok"},
        }},
        playstyle_fit=None,
        risk_level=None,
        decision_policy={},
        unknowns=[],
    )
    json.dumps(context, allow_nan=False)
    assert context["market"] == {}
    assert list(context["playstyle"]["features"]) == ["g" * 240]
    assert context["playstyle"]["features"]["g" * 240] == {"f" * 240: "ok"}


def test_build_report_context_selects_newest_history_and_joins_refreshed_forecasts():
    history_entries = [
        {
            "ts_code": "600487.SH",
            "date": f"2026-08-0{day}",
            "analyzed_at": f"2026-08-0{day}T10:00:00+08:00",
            "verdict": "偏空",
            "confidence": day,
            "forecast_outcome": None,
        }
        for day in range(1, 8)
    ]
    refreshed = [
        {
            "ts_code": "600487.SH",
            "analyzed_at": "2026-08-07T02:00:00+00:00",
            "verdict": "偏空",
            "stock_return_pct": 6.5,
            "benchmark_return_pct": 1.0,
            "excess_return_pct": 5.5,
            "outcome": "bull",
            "matured_at": "2026-08-21",
            "horizon_trading_days": 10,
            "hit": False,
            "policy_version": "decision-policy-v1",
            "analysis_text": "REFRESHED_FORECAST_DRAFT_SECRET",
        },
        {
            "ts_code": "000001.SZ",
            "analyzed_at": "2026-08-07T10:00:00+08:00",
            "verdict": "看多",
            "excess_return_pct": -99.0,
            "hit": True,
        },
    ]

    context = build_report_context(
        history_entries=history_entries,
        forecast_rows=refreshed,
        market_context={}, playstyle=None, playstyle_features={},
        playstyle_fit=None, risk_level=None, decision_policy={}, unknowns=[],
    )

    assert [item["date"] for item in context["history"]] == [
        "2026-08-07", "2026-08-06", "2026-08-05", "2026-08-04", "2026-08-03",
    ]
    assert context["history"][0]["forecast_outcome"] == {
        "outcome": "bull",
        "matured_at": "2026-08-21",
        "hit": False,
        "policy_version": "decision-policy-v1",
    }
    assert "return_pct" not in repr(context["history"])
    assert "horizon_trading_days" not in repr(context["history"])
    assert "REFRESHED_FORECAST_DRAFT_SECRET" not in repr(context)


def test_build_report_context_history_order_parses_timestamps_and_has_stable_fallbacks():
    context = build_report_context(
        history_entries=[
            {"analyzed_at": "invalid", "date": "2026-08-06", "confidence": 6},
            {"analyzed_at": "2026-08-07T09:00:00+08:00", "confidence": 70},
            {"analyzed_at": "2026-08-07T01:30:00+00:00", "confidence": 71},
            {"analyzed_at": "2026-08-05T10:00:00+08:00", "confidence": 50},
            {"analyzed_at": "2026-08-05T10:00:00+08:00", "confidence": 51},
            {"analyzed_at": None, "date": None, "confidence": 1},
        ],
        market_context={}, playstyle=None, playstyle_features={},
        playstyle_fit=None, risk_level=None, decision_policy={}, unknowns=[],
    )

    # 相同时间戳组内保留输入顺序（稳定排序），保证二次清洗幂等
    assert [item["confidence"] for item in context["history"]] == [71, 70, 6, 50, 51]


def test_build_report_context_history_cleaning_is_idempotent():
    # 正式报告节点会对已清洗的 history 再跑一次 build_report_context（analyze.py 的
    # sanitized_history 二次清洗）；二次排序不得把相同时间戳的组内顺序再次反转。
    history_entries = [
        {"analyzed_at": "2026-08-05T10:00:00+08:00", "confidence": 50, "verdict": "看空"},
        {"analyzed_at": "2026-08-05T10:00:00+08:00", "confidence": 51, "verdict": "看多"},
        {"analyzed_at": "2026-08-06T10:00:00+08:00", "confidence": 60, "verdict": "看多"},
    ]
    kwargs = dict(
        market_context={}, playstyle=None, playstyle_features={},
        playstyle_fit=None, risk_level=None, decision_policy={}, unknowns=[],
    )

    first = build_report_context(history_entries=history_entries, **kwargs)
    second = build_report_context(history_entries=first["history"], **kwargs)

    assert second["history"] == first["history"]


def test_build_report_context_excluded_claims_cannot_seed_directional_paraphrase():
    context = build_report_context(
        history_entries=[], market_context={}, playstyle=None,
        playstyle_features={}, playstyle_fit=None, risk_level=None,
        decision_policy={
            "excluded_claims": [{
                "evidence_id": "ev_old", "stance": "bear",
                "dimension": "capital", "nature": "fact", "hardness": 2,
                "adjusted_hardness": 1.0, "as_of": "2025-01-01",
                "frequency": "event", "reason": "stale_capital",
                "inference": "EXCLUDED_INFERENCE_OLD_DRAGON_TIGER",
                "fact": "EXCLUDED_FACT_PRIVATE_TEXT",
                "body": "EXCLUDED_BODY_PRIVATE_TEXT",
            }],
        },
        unknowns=[],
    )

    assert context["evidence_selection"]["excluded"] == [{
        "evidence_id": "ev_old", "dimension": "capital", "nature": "fact",
        "as_of": "2025-01-01", "frequency": "event", "reason": "stale_capital",
    }]
    assert "EXCLUDED_INFERENCE" not in repr(context)
    assert "EXCLUDED_FACT" not in repr(context)
    assert "EXCLUDED_BODY" not in repr(context)
    assert "bear" not in repr(context["evidence_selection"]["excluded"])
