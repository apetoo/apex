from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex import sector_sentiment as ss
from backend.routers import sector_sentiment as router_module


def _client(store):
    router_module.set_store_for_testing(store)
    app = FastAPI()
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
