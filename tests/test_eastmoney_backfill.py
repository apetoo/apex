from __future__ import annotations

import importlib
import hashlib
import json
from pathlib import Path

import pytest

from apex import sector_sentiment as ss
from apex.eastmoney_guba import CollectionResult


def _backfill_module():
    """Load the public historical-backfill boundary once it exists."""
    try:
        return importlib.import_module("apex.eastmoney_guba.backfill")
    except ModuleNotFoundError as exc:
        if exc.name == "apex.eastmoney_guba.backfill":
            pytest.fail("historical Eastmoney backfill module has not been implemented")
        raise


def _config(tmp_path: Path) -> dict:
    return {"sector_sentiment": {
        "enabled": True, "cache_dir": str(tmp_path),
        "platforms": ["bili", "dy", "eastmoney"],
        "taxonomy": [{"sector_id": "robot", "sector_name": "机器人", "taxonomy": "concept",
                       "aliases": ["机器人"], "eastmoney_forum_id": "bk0910"}],
        "eastmoney": {
            "enabled": True, "phase": "shadow", "access_mode": "public",
            "lookback_hours": 24, "overlap_hours": 2, "constituent_count": 1,
            "posts_per_sector_forum": 10, "posts_per_constituent_forum": 10,
            "comments_per_post": 10, "concurrent_requests_per_domain": 1,
            "download_delay_seconds": 2, "timeout_seconds": 20,
            "collection_timeout_seconds": 900, "retry_times": 3,
            "circuit_breaker_failures": 5, "max_requests": 20,
            "requests_per_target": 10, "schema_version": "eastmoney-public-v1",
            "target_pool_version": "eastmoney-target-v1", "author_salt": "test-salt",
            "endpoints": {
                "list_url_template": "https://guba.eastmoney.com/list,{forum_id}.html",
                "list_next_url_template": "https://guba.eastmoney.com/list,{forum_id}_{page}.html",
                "detail_url_template": "https://guba.eastmoney.com/news,{forum_id},{content_id}.html",
                "comments_url_template": "https://guba.eastmoney.com/comments/{content_id}",
            }, "minimum_posts": 1, "minimum_authors": 1, "minimum_parse_success_rate": 0.8,
        },
    }}


def _qualified_runner(manifest_path: Path, report_path: Path, *, timeout_seconds: int):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    Path(manifest["output_file"]).parent.mkdir(parents=True, exist_ok=True)
    Path(manifest["output_file"]).write_text(json.dumps({
        "platform": "eastmoney", "content_id": "post-1", "comment_id": "",
        "author_hash": "author-1", "text": "机器人板块看多",
        "published_at": "2026-08-17T19:00:00+08:00",
        "collected_at": "2026-08-17T20:00:00+08:00",
        "url": "https://guba.eastmoney.com/news,bk0910,post-1.html",
        "sector_ids": ["robot"], "source_type": "sector_forum",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    Path(report_path).write_text(json.dumps({"parser_successes": 1, "parser_errors": 0,
                                              "request_successes": 1, "request_errors": 0}), encoding="utf-8")
    return CollectionResult("ok", 1, 1, 0)


def _constituents(_taxonomy: list[dict], _trade_date: str):
    return {"robot": [{"stock_code": "000001", "turnover_20d": 100.0}]}


def test_backfill_rejects_as_of_without_timezone():
    backfill = _backfill_module()
    with pytest.raises(ValueError, match="timezone"):
        backfill.validate_backfill_request(trade_date="2026-08-17", as_of="2026-08-17T20:00:00")


def test_backfill_rejects_shadow_count_without_audited_reason():
    backfill = _backfill_module()
    with pytest.raises(ValueError, match="reason"):
        backfill.validate_backfill_request(trade_date="2026-08-17", as_of="2026-08-17T20:00:00+08:00",
                                           count_shadow_day=True, reason="  ")


def test_default_backfill_is_eastmoney_only_and_does_not_count_shadow_day(tmp_path: Path):
    backfill = _backfill_module()
    result = backfill.run_eastmoney_backfill(_config(tmp_path), trade_date="2026-08-17",
        as_of="2026-08-17T20:00:00+08:00", eastmoney_runner=_qualified_runner,
        eastmoney_constituent_provider=_constituents)
    assert result["backfill"] is True and result["counted_shadow_day"] is False and result["scored"] is False
    assert result["collection"]["eastmoney"]["posts"] == 1
    assert {job["platform"] for job in result["collection"]["jobs"]} == {"eastmoney"}
    stored = ss.open_store(tmp_path).collection_summary("2026-08-17")
    assert stored["platforms"]["eastmoney"]["backfill"] is True
    assert stored["platforms"]["eastmoney"]["shadow_qualified"] is False


def test_reasoned_qualified_backfill_records_an_audit_and_can_count_shadow_day(tmp_path: Path):
    backfill = _backfill_module()
    result = backfill.run_eastmoney_backfill(_config(tmp_path), trade_date="2026-08-17",
        as_of="2026-08-17T20:00:00+08:00", count_shadow_day=True, reason="page parser fix replay",
        eastmoney_runner=_qualified_runner, eastmoney_constituent_provider=_constituents)
    assert result["counted_shadow_day"] is True
    stored = ss.open_store(tmp_path).collection_summary("2026-08-17")
    audit = stored["platforms"]["eastmoney"]["backfill_audit"]
    assert audit["count_shadow_day"] is True and audit["reason"] == "page parser fix replay"
    assert stored["platforms"]["eastmoney"]["shadow_qualified"] is True
    assert ss.open_store(tmp_path).eastmoney_shadow_progress()["qualified_days"] == 1


def test_offline_replay_without_saved_batch_is_a_clear_error(tmp_path: Path):
    backfill = _backfill_module()
    with pytest.raises(FileNotFoundError, match="saved.*batch|offline.*batch"):
        backfill.run_eastmoney_backfill(_config(tmp_path), trade_date="2026-08-17",
            as_of="2026-08-17T20:00:00+08:00", offline_replay=True)


def test_offline_replay_uses_saved_list_response_without_provider_or_network(tmp_path: Path):
    """Catches replay falling through to live constituent/provider collection."""
    backfill = _backfill_module()
    batch = tmp_path / "raw" / "2026-08-17" / "eastmoney" / "saved-live-page"
    responses = batch / "responses"
    responses.mkdir(parents=True)
    list_url = "https://guba.eastmoney.com/list,bk0910.html"
    body = b'''<html><body><script>var article_list={"re":[{
      "post_id":1760184053,"post_title":"robot sentiment",
      "stockbar_code":"bk0910","user_id":"saved-author",
      "post_click_count":64,"post_comment_count":1,"post_like_count":0,
      "post_publish_time":"2026-08-17 19:13:59",
      "post_last_time":"2026-08-17 19:13:59","post_type":20
    }]};</script></body></html>'''
    response_id = hashlib.sha256(b"list\0" + list_url.encode("utf-8") + b"\0" + body).hexdigest()
    (responses / f"{response_id}.body").write_bytes(body)
    manifest = {
        "trade_date": "2026-08-17", "batch_id": "saved-live-page",
        "collected_at": "2026-08-17T20:00:00+08:00",
        "window_start": "2026-08-16T18:00:00+08:00",
        "output_file": str(batch / "records.jsonl"), "author_salt": "test-salt",
        "raw_response_dir": str(responses), "representative_constituents": {},
        "missing_targets": [], "settings": {"max_requests": 20, "requests_per_target": 10,
            "posts_per_target": 10, "comments_per_post": 10, "download_delay_seconds": 2,
            "timeout_seconds": 20, "retry_times": 3, "concurrent_requests_per_domain": 1,
            "circuit_breaker_failures": 5},
        "jobs": [{"kind": "list", "target_id": "bk0910", "url": list_url,
            "list_next_url_template": "https://guba.eastmoney.com/list,bk0910_{page}.html",
            "detail_url_template": "https://guba.eastmoney.com/news,bk0910,{content_id}.html",
            "comments_url_template": "https://guba.eastmoney.com/comments/{content_id}",
            "window_start": "2026-08-16T18:00:00+08:00", "page": 1, "posts_limit": 10,
            "forum_id": "bk0910", "sector_id": "robot", "source_type": "sector_forum",
            "stock_code": None, "pool_version": "eastmoney-target-v1"}],
    }
    (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = backfill.run_eastmoney_backfill(
        _config(tmp_path), trade_date="2026-08-17", as_of="2026-08-17T20:00:00+08:00",
        offline_replay=True,
    )

    assert result["collection"]["eastmoney"]["posts"] == 1
    assert {job["platform"] for job in result["collection"]["jobs"]} == {"eastmoney"}
    assert result["counted_shadow_day"] is False
