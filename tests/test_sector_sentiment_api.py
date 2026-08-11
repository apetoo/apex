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
