"""Read-only sector sentiment shadow-alert API."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from apex import config
from apex import sector_sentiment as ss


router = APIRouter(prefix="/sector-sentiment", tags=["sector-sentiment"])
_TEST_STORE: ss.SentimentStore | None = None


def set_store_for_testing(store: ss.SentimentStore | None) -> None:
    global _TEST_STORE
    _TEST_STORE = store


def _store() -> ss.SentimentStore:
    if _TEST_STORE is not None:
        return _TEST_STORE
    cfg = config.get() or {}
    cache = (cfg.get("sector_sentiment") or {}).get("cache_dir", "~/.stock-sentiment")
    return ss.open_store(Path(str(cache)))


def _meta(store: ss.SentimentStore, date: str | None) -> dict:
    resolved = date or store.latest_date()
    scores = store.scores_for_date(resolved) if resolved else []
    model_version = scores[0]["model_version"] if scores else ss.MODEL_VERSION
    return {"as_of": resolved, "model_version": model_version, "rule_version": ss.RULE_VERSION,
            "prompt_version": ss.PROMPT_VERSION, "dictionary_version": ss.DICTIONARY_VERSION,
            "mapping_version": ss.MAPPING_VERSION}


def _expected_platforms() -> int:
    cfg = config.get() or {}
    return max(1, len((cfg.get("sector_sentiment") or {}).get("platforms", ["bili", "dy"])))


def _quality_meta(store: ss.SentimentStore, date: str | None) -> dict:
    resolved = date or store.latest_date()
    sectors = store.scores_for_date(resolved) if resolved else []
    run = store.collection_summary(resolved if date else None)
    if run["status"] == "no_data":
        present = {platform for sector in sectors for platform in sector["platforms"]}
        coverage = min(1.0, len(present) / _expected_platforms())
    else:
        coverage = float(run["coverage"])
    quality = "ok" if sectors and coverage >= 1 else "no_data" if not sectors else "degraded"
    stale = bool(resolved and run.get("trade_date") and resolved < run["trade_date"])
    return {**_meta(store, resolved), "coverage": coverage, "data_quality": quality,
            "stale": stale}


def _retrieval_funnel(store: ss.SentimentStore, date: str | None) -> dict | None:
    """Return daily accounting, preserving absent telemetry as null."""
    return store.collection_summary(date).get("funnel")


def _public_creator(creator: dict) -> dict:
    """Limit reviewer responses to the public creator contract and content evidence."""
    fields = (
        "platform", "creator_id", "display_name", "status", "financial_ratio",
        "valid_content_count", "sector_ids", "last_discovered_at", "evidence",
        "last_collection_error",
    )
    value = {field: creator[field] for field in fields}
    value["evidence"] = [
        {"text": ss._redact_text(item.get("text"), 200)}
        for item in creator.get("evidence", []) if isinstance(item, dict) and item.get("text")
    ]
    if value["last_collection_error"]:
        value["last_collection_error"] = "采集失败，请稍后重试"
    return value


@router.get("/overview")
def overview(date: str | None = Query(None)):
    store = _store()
    resolved = date or store.latest_date()
    sectors = store.scores_for_date(resolved) if resolved else []
    for sector in sectors:
        sector["state"] = store.state_as_of(sector["sector_id"], resolved)
    events = [event for event in store.list_events() if event["trade_date"] == resolved]
    meta = _quality_meta(store, resolved)
    return {**meta,
            "sectors": sectors, "changes": events, "shadow_mode": True,
            "retrieval_funnel": _retrieval_funnel(store, resolved)}


@router.get("/events")
def events():
    store = _store()
    return {**_quality_meta(store, store.latest_date()), "events": store.list_events()}


@router.get("/validation")
def validation():
    store = _store()
    rows = store.validation_rows()
    run = store.collection_summary()
    trading_days = int(run["trading_days"])
    short = ss.evaluate_layer(rows, "short_hit")
    swing = ss.evaluate_layer(rows, "swing_hit")
    verdicts = {short["verdict"], swing["verdict"]}
    go_no_go = "GO" if verdicts == {"GO"} else "NO-GO" if verdicts == {"NO-GO"} else "MIXED" if "PENDING" not in verdicts else "PENDING"
    return {**_quality_meta(store, store.latest_date()), "status": "ready" if trading_days >= 60 else "accumulating",
            "trading_days": trading_days, "target_days": 60,
            "short": short, "swing": swing, "go_no_go": go_no_go}


@router.get("/creators")
def creators(status: Literal["candidate", "approved", "rejected"] | None = Query(None)):
    store = _store()
    return {
        **_quality_meta(store, store.latest_date()),
        "creators": [_public_creator(creator) for creator in store.list_creators(status)],
    }


def _moderate(platform: str, creator_id: str, action: Literal["approve", "reject", "restore"]):
    store = _store()
    creator = store.moderate_creator(platform, creator_id, action)
    return {**_quality_meta(store, store.latest_date()), "creator": _public_creator(creator)}


@router.post("/creators/{platform}/{creator_id}/approve")
def approve_creator(platform: str, creator_id: str):
    return _moderate(platform, creator_id, "approve")


@router.post("/creators/{platform}/{creator_id}/reject")
def reject_creator(platform: str, creator_id: str):
    return _moderate(platform, creator_id, "reject")


@router.post("/creators/{platform}/{creator_id}/restore")
def restore_creator(platform: str, creator_id: str):
    return _moderate(platform, creator_id, "restore")


@router.get("/{sector_id}")
def detail(sector_id: str, date: str | None = Query(None)):
    store = _store()
    history = store.scores_for_sector(sector_id)
    if date:
        history = [row for row in history if row["trade_date"] <= date]
    if not history:
        raise HTTPException(status_code=404, detail="sector not found")
    latest = history[-1]
    state = store.get_state(sector_id)
    meta = _quality_meta(store, latest["trade_date"])
    quality = "ok" if meta["data_quality"] == "ok" and _detail_ok(latest) else "degraded"
    return {**meta, "data_quality": quality, "sector": latest,
            "state": state, "history": history[-60:],
            "retrieval_funnel": _retrieval_funnel(store, latest["trade_date"])}


def _detail_ok(score: dict) -> bool:
    return (len(score["platforms"]) >= ss.MIN_PLATFORMS
            and score["independent_authors"] >= ss.MIN_AUTHORS
            and score["mapping_confidence"] >= ss.MIN_MAPPING_CONFIDENCE)
