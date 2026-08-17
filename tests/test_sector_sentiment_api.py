import json
from datetime import date, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex import sector_sentiment as ss
from backend.core.errors import register_exception_handlers
from backend.routers import sector_sentiment as router_module


def _client(store):
    router_module.set_store_for_testing(store)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router_module.router, prefix="/api")
    return TestClient(app)


def test_overview_exposes_quality_versions_and_event_changes(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.save_daily_score({
        "trade_date": "2026-08-10", "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "platforms": ["bili", "eastmoney"], "independent_authors": 20,
        "mapping_confidence": 0.9, "sentiment_extreme": 0.95,
        "attention_acceleration": 0.92, "consensus_crowding": 0.8,
        "market_divergence": 0.2, "short_risk": 66, "swing_risk": 48,
        "coverage_quality": {"qualified": True},
    })
    ss.advance_alerts(store, "2026-08-10")

    payload = _client(store).get("/api/sector-sentiment/overview?date=2026-08-10").json()

    assert payload["as_of"] == "2026-08-10"
    assert payload["model_version"] == ss.MODEL_VERSION
    assert payload["rule_version"] == ss.RULE_VERSION
    assert payload["data_quality"] == "ok"
    assert payload["sectors"][0]["state"] == "observe"
    assert payload["changes"][0]["event_type"] == "opened"


def test_detail_and_validation_return_empty_safe_payloads(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    client = _client(store)

    assert client.get("/api/sector-sentiment/concept:missing").status_code == 404
    validation = client.get("/api/sector-sentiment/validation").json()
    assert validation["status"] == "accumulating"
    assert validation["trading_days"] == 0
    assert validation["go_no_go"] == "PENDING"


def _candidate_store(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    records = [
        {
            "platform": "bili", "content_id": f"video-{index}",
            "author_id": "up-1", "author_name": "财经小王",
            "text": "机器人板块资金流入", "relevance_score": 0.9,
            "sector_ids": ["concept:robot"],
        }
        for index in range(3)
    ]
    store.discover_creator_candidates(records, {"candidate_min_contents": 3})
    return store


def test_creator_endpoints_list_and_moderate(tmp_path):
    client = _client(_candidate_store(tmp_path))

    listed = client.get("/api/sector-sentiment/creators?status=candidate")
    assert listed.status_code == 200
    assert listed.json()["creators"][0]["creator_id"] == "up-1"

    approved = client.post("/api/sector-sentiment/creators/bili/up-1/approve")
    assert approved.status_code == 200
    assert approved.json()["creator"]["status"] == "approved"
    assert client.get("/api/sector-sentiment/creators?status=approved").json()["creators"]
    assert client.get("/api/sector-sentiment/creators?status=unknown").status_code == 422
    assert client.post("/api/sector-sentiment/creators/bili/missing/approve").status_code == 404


def test_detail_exposes_retrieval_funnel(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.save_daily_score({
        "trade_date": "2026-08-10", "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "platforms": ["bili", "eastmoney"], "independent_authors": 20,
        "mapping_confidence": 0.9, "sentiment_extreme": 0.95,
        "attention_acceleration": 0.92, "consensus_crowding": 0.8,
        "market_divergence": 0.2, "short_risk": 66, "swing_risk": 48,
    })
    store.record_collection({
        "trade_date": "2026-08-10", "coverage": 1.0, "search_coverage": 1.0,
        "creator_coverage": 1.0, "platforms": {}, "funnel": {
            "raw_recalled": 10, "financial_relevant": 4, "filtered": 6,
            "search_sources": 3, "creator_sources": 1,
        },
    })

    response = _client(store).get("/api/sector-sentiment/concept:robot")
    assert response.status_code == 200
    assert response.json()["retrieval_funnel"] == {
        "raw_recalled": 10, "financial_relevant": 4, "filtered": 6,
        "search_sources": 3, "creator_sources": 1,
    }


def test_historical_score_without_collection_telemetry_returns_null_funnel(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.save_daily_score({
        "trade_date": "2026-08-09", "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "platforms": ["bili", "eastmoney"], "independent_authors": 20,
        "mapping_confidence": 0.9, "sentiment_extreme": 0.95,
        "attention_acceleration": 0.92, "consensus_crowding": 0.8,
        "market_divergence": 0.2, "short_risk": 66, "swing_risk": 48,
    })
    client = _client(store)

    assert client.get("/api/sector-sentiment/overview?date=2026-08-09").json()["retrieval_funnel"] is None
    assert client.get("/api/sector-sentiment/concept:robot?date=2026-08-09").json()["retrieval_funnel"] is None


def test_detail_keeps_collection_coverage_instead_of_sector_platform_ratio(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.save_daily_score({
        "trade_date": "2026-08-10", "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "platforms": ["bili", "eastmoney"], "independent_authors": 20,
        "mapping_confidence": 0.9, "sentiment_extreme": 0.95,
        "attention_acceleration": 0.92, "consensus_crowding": 0.8,
        "market_divergence": 0.2, "short_risk": 66, "swing_risk": 48,
    })
    store.record_collection({
        "trade_date": "2026-08-10", "coverage": 0.5,
        "search_coverage": 0.5, "creator_coverage": 1.0,
        "platforms": {}, "funnel": None,
    })

    payload = _client(store).get("/api/sector-sentiment/concept:robot").json()

    assert payload["coverage"] == 0.5
    assert payload["data_quality"] == "degraded"


def test_creator_not_found_uses_the_global_domain_error_mapping(tmp_path):
    response = _client(ss.SentimentStore(tmp_path / "sentiment.sqlite3")).post(
        "/api/sector-sentiment/creators/bili/missing/approve",
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "creator not found"}


def test_creator_api_returns_redacted_minimal_evidence_without_content_ids(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.discover_creator_candidates([{
        "platform": "bili", "content_id": "private-content-id", "author_id": "up-1",
        "author_name": "财经小王", "text": "邮箱 test@example.com 手机13800138000 微信wx_secret",
        "published_at": "2026-08-10T08:00:00+00:00", "relevance_score": 0.9,
        "relevance_decision": "accepted", "relevance_reasons": ["path:entity_and_finance"],
        "sector_ids": ["concept:robot"],
    }], {
        "candidate_min_contents": 1,
        "query_version": "sector-finance-query-v1",
        "relevance_version": "sector-finance-relevance-v1",
        "creator_rule_version": "sector-finance-creator-v1",
        "config_hash": "policy-hash",
    })

    creator = _client(store).get("/api/sector-sentiment/creators?status=candidate").json()["creators"][0]

    assert set(creator) == {
        "platform", "creator_id", "display_name", "status", "financial_ratio",
        "valid_content_count", "sector_ids", "last_discovered_at", "evidence",
        "last_collection_error",
    }
    assert "content_id" not in creator["evidence"][0]
    assert "private-content-id" not in json.dumps(creator, ensure_ascii=False)
    assert "example.com" not in json.dumps(creator, ensure_ascii=False)
    assert "13800138000" not in json.dumps(creator, ensure_ascii=False)
    assert "wx_secret" not in json.dumps(creator, ensure_ascii=False)


def test_creator_api_does_not_expose_collection_exception_paths(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.discover_creator_candidates([{
        "platform": "bili", "content_id": "content", "author_id": "opaque",
        "text": "机器人板块资金流入", "relevance_score": 0.9,
        "relevance_decision": "accepted", "sector_ids": ["concept:robot"],
    }], {"candidate_min_contents": 1})
    store.set_creator_collection_error(
        "bili", "opaque", "FileNotFoundError: /Users/private/MediaCrawler/main.py",
    )

    creator = _client(store).get(
        "/api/sector-sentiment/creators?status=candidate",
    ).json()["creators"][0]

    assert creator["last_collection_error"] == "采集失败，请稍后重试"
    assert "/Users/private" not in json.dumps(creator, ensure_ascii=False)


def test_validation_resets_to_one_day_when_a_new_policy_cohort_starts(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    start = date(2026, 1, 1)
    for offset in range(60):
        store.record_collection({
            "trade_date": (start + timedelta(days=offset)).isoformat(),
            "coverage": 1.0, "search_coverage": 1.0, "creator_coverage": 1.0,
            "platforms": {}, "funnel": {}, "config_hash": "policy-v1",
        })
    store.record_collection({
        "trade_date": (start + timedelta(days=60)).isoformat(),
        "coverage": 1.0, "search_coverage": 1.0, "creator_coverage": 1.0,
        "platforms": {}, "funnel": {}, "config_hash": "policy-v2",
    })

    payload = _client(store).get("/api/sector-sentiment/validation").json()

    assert payload["trading_days"] == 1
    assert payload["status"] == "accumulating"
    assert payload["short"]["verdict"] == "PENDING"
    assert payload["swing"]["verdict"] == "PENDING"


def test_overview_exposes_safe_eastmoney_telemetry_and_shadow_progress(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    for day in ("2026-08-10", "2026-08-11"):
        store.record_collection({
            "trade_date": day, "coverage": 1.0, "search_coverage": 1.0,
            "creator_coverage": 1.0, "config_hash": "eastmoney-policy-v1",
                "platforms": {"eastmoney": {
                    "phase": "shadow", "shadow_qualified": True,
                "current_status": "ok", "display_status": "ok", "posts": 12,
                "comments": 34, "independent_authors": 9,
                "sector_forum_records": 30, "constituent_forum_records": 16,
                "request_success_rate": 0.98, "parse_success_rate": 0.96,
                "quota_exhausted": False, "circuit_open": False,
                "as_of": f"{day}T10:00:00Z", "last_success_at": f"{day}T10:00:00Z",
                "path": "/Users/private/records.jsonl", "manifest_path": "/tmp/secret.json",
                "collector": {"cookies": "secret"},
            }}, "funnel": {},
        })
    store.save_daily_score({
        "trade_date": "2026-08-11", "sector_id": "concept:robot",
        "sector_name": "机器人", "taxonomy": "concept",
        "platforms": ["bili", "eastmoney"], "independent_authors": 20,
        "mapping_confidence": 0.9, "sentiment_extreme": 0.8,
        "attention_acceleration": 0.8, "consensus_crowding": 0.8,
        "market_divergence": 0.2, "short_risk": 60, "swing_risk": 50,
    })

    payload = _client(store).get("/api/sector-sentiment/overview").json()

    assert payload["eastmoney"] == {
        "current_status": "ok", "display_status": "ok", "posts": 12,
        "first_level_comments": 34, "independent_authors": 9,
        "sector_forum_records": 30, "constituent_forum_records": 16,
        "request_success_rate": 0.98, "parse_success_rate": 0.96,
        "quota_exhausted": False, "circuit_open": False,
        "schema_changed": False, "blocked": False, "stale": False,
        "current_attempt_at": "2026-08-11T10:00:00Z",
        "latest_success_at": "2026-08-11T10:00:00Z",
        "phase": "shadow", "shadow_attempt_days": 2, "shadow_qualified_days": 2,
        "shadow_days": 2, "shadow_target_days": 14,
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "/Users/private" not in encoded
    assert "cookies" not in encoded


def test_detail_uses_stale_display_status_but_keeps_current_failure_audit(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.record_collection({
        "trade_date": "2026-08-11", "coverage": 0.5, "search_coverage": 1.0,
        "creator_coverage": 1.0, "platforms": {"eastmoney": {
            "current_status": "blocked", "display_status": "stale", "stale": True,
            "posts": 0, "comments": 0, "independent_authors": 0,
            "request_success_rate": 0.2, "parse_success_rate": 0.0,
            "as_of": "2026-08-11T10:00:00Z",
            "last_success": {"as_of": "2026-08-10T10:00:00Z"},
            "message": "Cookie secret at /Users/private/cache",
        }}, "funnel": {},
    })
    store.save_daily_score({
        "trade_date": "2026-08-11", "sector_id": "concept:robot",
        "sector_name": "机器人", "taxonomy": "concept",
        "platforms": ["bili", "eastmoney"], "independent_authors": 20,
        "mapping_confidence": 0.9, "sentiment_extreme": 0.8,
        "attention_acceleration": 0.8, "consensus_crowding": 0.8,
        "market_divergence": 0.2, "short_risk": 60, "swing_risk": 50,
        "evidence": [{"platform": "eastmoney", "text": "联系13800138000 看多机器人",
                      "stance": 1, "url": "https://guba.eastmoney.com/news,bk0910,1.html"}],
    })

    payload = _client(store).get("/api/sector-sentiment/concept:robot").json()

    assert payload["eastmoney"]["display_status"] == "stale"
    assert payload["eastmoney"]["current_status"] == "blocked"
    assert payload["eastmoney"]["latest_success_at"] == "2026-08-10T10:00:00Z"
    assert payload["data_quality"] == "degraded"
    assert payload["sector"]["evidence"][0]["text"] == "联系[电话已脱敏] 看多机器人"
    assert payload["sector"]["evidence"][0]["url"].startswith("https://guba.eastmoney.com/")
    assert "private" not in json.dumps(payload, ensure_ascii=False)


def test_eastmoney_shadow_progress_counts_its_own_attempt_and_qualified_dates(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    for day, qualified in (("2026-08-10", True), ("2026-08-11", False)):
        store.record_collection({"trade_date": day, "coverage": 1,
            "search_coverage": 1, "creator_coverage": 1,
            "platforms": {"eastmoney": {"phase": "shadow",
                "current_status": "ok" if qualified else "blocked",
                "display_status": "ok" if qualified else "blocked",
                "shadow_qualified": qualified}}, "funnel": {}})
    payload = _client(store).get("/api/sector-sentiment/overview").json()["eastmoney"]
    assert payload["phase"] == "shadow"
    assert payload["shadow_attempt_days"] == 2
    assert payload["shadow_qualified_days"] == 1
    assert payload["shadow_days"] == 1


def _promoted_score(trade_date: str) -> dict:
    return {
        "trade_date": trade_date, "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "platforms": ["bili", "eastmoney"], "independent_authors": 20,
        "mapping_confidence": 0.9, "sentiment_extreme": 0.95,
        "attention_acceleration": 0.92, "consensus_crowding": 0.8,
        "market_divergence": 0.2, "short_risk": 66, "swing_risk": 48,
        "coverage_quality": {"qualified": True},
    }


def test_promoted_collection_failure_uses_prior_score_but_current_audit_for_overview_and_detail(tmp_path):
    """Catches an API that either hides today's failed audit or presents stale scores as current."""
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.record_collection({
        "trade_date": "2026-08-10", "coverage": 1.0, "search_coverage": 1.0,
        "creator_coverage": 1.0, "platforms": {"eastmoney": {
            "phase": "promoted", "current_status": "ok", "status": "ok",
            "as_of": "2026-08-10T10:00:00Z",
        }}, "funnel": {},
    })
    store.save_daily_score(_promoted_score("2026-08-10"))
    store.record_collection({
        "trade_date": "2026-08-11", "coverage": 0.0, "search_coverage": 1.0,
        "creator_coverage": 1.0, "platforms": {"eastmoney": {
            "phase": "promoted", "current_status": "blocked", "status": "blocked",
            "as_of": "2026-08-11T10:00:00Z",
            "promotion_audit": {
                "required_qualified_days": 14, "observed_qualified_days": 3,
                "eligible_by_history": False, "override_used": True,
                "override_reason": "operator reviewed collector evidence",
            },
        }}, "funnel": {},
    })
    client = _client(store)

    assert store.latest_successful_eastmoney_date("2026-08-11") == "2026-08-10"
    overview = client.get("/api/sector-sentiment/overview").json()
    detail = client.get("/api/sector-sentiment/concept:robot").json()

    for payload in (overview, detail):
        assert payload["as_of"] == "2026-08-11"
        assert payload["score_as_of"] == "2026-08-10"
        assert payload["stale"] is True
        assert payload["data_quality"] == "degraded"
        assert payload["eastmoney"]["current_status"] == "blocked"
        assert payload["eastmoney"]["current_attempt_at"] == "2026-08-11T10:00:00Z"
        assert payload["eastmoney"]["override_used"] is True
        assert "override_reason" not in payload["eastmoney"]
        assert "operator reviewed collector evidence" not in json.dumps(payload, ensure_ascii=False)
    assert overview["sectors"][0]["trade_date"] == "2026-08-10"
    assert detail["sector"]["trade_date"] == "2026-08-10"


def test_promoted_same_day_retry_failure_keeps_earlier_successful_score_stale(tmp_path):
    """Catches a retry hiding a successful promoted score from earlier on the same date."""
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.record_collection({
        "trade_date": "2026-08-11", "coverage": 1.0, "search_coverage": 1.0,
        "creator_coverage": 1.0, "platforms": {"eastmoney": {
            "phase": "promoted", "current_status": "ok", "status": "ok",
            "as_of": "2026-08-11T09:00:00Z",
        }}, "funnel": {},
    })
    store.save_daily_score(_promoted_score("2026-08-11"))
    store.record_collection({
        "trade_date": "2026-08-11", "coverage": 0.0, "search_coverage": 1.0,
        "creator_coverage": 1.0, "platforms": {"eastmoney": {
            "phase": "promoted", "current_status": "failed", "status": "failed",
            "as_of": "2026-08-11T16:00:00Z",
        }}, "funnel": {},
    })
    client = _client(store)

    assert store.latest_successful_eastmoney_date("2026-08-11") == "2026-08-11"
    for payload in (
        client.get("/api/sector-sentiment/overview").json(),
        client.get("/api/sector-sentiment/concept:robot").json(),
    ):
        assert payload["as_of"] == "2026-08-11"
        assert payload["score_as_of"] == "2026-08-11"
        assert payload["stale"] is True
        assert payload["data_quality"] == "degraded"
        assert payload["eastmoney"]["current_status"] == "failed"
    assert client.get("/api/sector-sentiment/overview").json()["sectors"][0]["state"] == "insufficient_data"


def test_promoted_collection_failure_without_prior_score_is_insufficient_data_not_normal(tmp_path):
    """Catches a first promoted failure being rendered as harmless because no alert event exists."""
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.save_daily_score({**_promoted_score("2026-08-11"), "coverage_quality": {"qualified": False}})
    store.record_collection({
        "trade_date": "2026-08-11", "coverage": 0.0, "search_coverage": 1.0,
        "creator_coverage": 1.0, "platforms": {"eastmoney": {
            "phase": "promoted", "current_status": "failed", "status": "failed",
            "as_of": "2026-08-11T10:00:00Z",
        }}, "funnel": {},
    })

    payload = _client(store).get("/api/sector-sentiment/overview").json()

    assert payload["data_quality"] == "degraded"
    assert payload["stale"] is False
    assert payload["sectors"][0]["state"] == "insufficient_data"
