from apex.report_context import build_report_context


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
