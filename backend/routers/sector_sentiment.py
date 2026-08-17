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


def _expected_platforms() -> int:
    # API read paths remain usable in a fresh install and unit tests before an
    # operator has created config.yaml.  Deliberately narrow this fallback to
    # the absent-file case: malformed YAML and other configuration errors must
    # still surface rather than being mistaken for the legacy default policy.
    try:
        cfg = config.get() or {}
    except FileNotFoundError:
        cfg = {}
    return max(1, len((cfg.get("sector_sentiment") or {}).get("platforms", ["bili", "dy"])))


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
    return ss.sector_sentiment_overview(
        _store(), date, expected_platforms=_expected_platforms(),
    )


@router.get("/events")
def events():
    store = _store()
    return {**ss.sector_sentiment_quality_meta(
        store, store.latest_date(), expected_platforms=_expected_platforms(),
    ), "events": store.list_events()}


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
    return {**ss.sector_sentiment_quality_meta(
        store, store.latest_date(), expected_platforms=_expected_platforms(),
    ), "status": "ready" if trading_days >= 60 else "accumulating",
            "trading_days": trading_days, "target_days": 60,
            "short": short, "swing": swing, "go_no_go": go_no_go}


@router.get("/creators")
def creators(status: Literal["candidate", "approved", "rejected"] | None = Query(None)):
    store = _store()
    return {
        **ss.sector_sentiment_quality_meta(
            store, store.latest_date(), expected_platforms=_expected_platforms(),
        ),
        "creators": [_public_creator(creator) for creator in store.list_creators(status)],
    }


def _moderate(platform: str, creator_id: str, action: Literal["approve", "reject", "restore"]):
    store = _store()
    creator = store.moderate_creator(platform, creator_id, action)
    return {**ss.sector_sentiment_quality_meta(
        store, store.latest_date(), expected_platforms=_expected_platforms(),
    ), "creator": _public_creator(creator)}


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
    payload = ss.sector_sentiment_detail(
        _store(), sector_id, date, expected_platforms=_expected_platforms(),
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="sector not found")
    return payload
