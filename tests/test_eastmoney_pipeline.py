from __future__ import annotations

import json
import copy
from pathlib import Path

import pytest

from apex import sector_sentiment as ss
from apex.eastmoney_guba import CollectionResult
from apex.eastmoney_guba.pipeline import (
    build_frozen_manifest,
    collect_eastmoney,
    collector_health,
    grouped_coverage,
    validate_eastmoney_config,
)


def _config() -> dict:
    return {
        "enabled": True,
        "phase": "promoted",
        "access_mode": "public",
        "lookback_hours": 24,
        "overlap_hours": 2,
        "constituent_count": 5,
        "posts_per_sector_forum": 100,
        "posts_per_constituent_forum": 50,
        "comments_per_post": 50,
        "concurrent_requests_per_domain": 1,
        "download_delay_seconds": 2,
        "timeout_seconds": 20,
        "collection_timeout_seconds": 900,
        "retry_times": 3,
        "circuit_breaker_failures": 5,
        "max_requests": 500,
        "requests_per_target": 100,
        "schema_version": "eastmoney-public-v1",
        "target_pool_version": "eastmoney-target-v1",
        "author_salt": "local-only",
        "promotion_override_reason": "fixture promotion review approved",
        "endpoints": {
            "list_url_template": "https://guba.eastmoney.com/list,{forum_id}.html",
            "list_next_url_template": "https://guba.eastmoney.com/list,{forum_id}_{page}.html",
            "detail_url_template": "https://guba.eastmoney.com/news,{forum_id},{content_id}.html",
            "comments_url_template": "https://guba.eastmoney.com/comments/{content_id}",
        },
        "minimum_posts": 1,
        "minimum_authors": 1,
        "minimum_parse_success_rate": 0.8,
    }


@pytest.mark.parametrize(("field", "value"), [
    ("access_mode", "login"), ("max_requests", -1),
    ("comments_per_post", True), ("schema_version", ""),
])
def test_eastmoney_config_rejects_unsafe_or_unbounded_values(field, value):
    config = _config()
    config[field] = value
    with pytest.raises(ValueError):
        validate_eastmoney_config(config)


def test_enabled_eastmoney_rejects_unknown_phase_and_placeholder_salt():
    config = _config()
    config["phase"] = "live"
    with pytest.raises(ValueError, match="phase"):
        validate_eastmoney_config(config)
    config = _config()
    config["author_salt"] = "CHANGE-ME-LOCAL-ONLY"
    with pytest.raises(ValueError, match="author_salt"):
        validate_eastmoney_config(config)


def test_request_and_process_timeouts_are_distinct_and_positive():
    config = _config()
    config["timeout_seconds"] = 0
    with pytest.raises(ValueError):
        validate_eastmoney_config(config)
    config = _config()
    config["collection_timeout_seconds"] = 0
    with pytest.raises(ValueError):
        validate_eastmoney_config(config)


def test_parse_health_uses_real_counters_not_request_guess():
    health = collector_health({"parser_successes": 8, "parser_errors": 2,
                               "request_successes": 9, "request_errors": 1})
    assert health["parse_success_rate"] == pytest.approx(0.8)
    assert health["request_success_rate"] == pytest.approx(0.9)


def test_collection_timeout_is_forwarded_and_each_retry_gets_unique_batch(tmp_path: Path):
    seen = []
    def runner(manifest_path, report_path, *, timeout_seconds):
        seen.append((Path(manifest_path).parent.name, timeout_seconds))
        Path(report_path).write_text(json.dumps({
            "parser_successes": 1, "parser_errors": 0,
            "request_successes": 1, "request_errors": 0,
        }))
        return CollectionResult("empty_valid", 0, 1, 0)
    kwargs = dict(cache_dir=tmp_path, config=_config(), taxonomy=[], constituents={},
                  trade_date="2026-08-12", collected_at="2026-08-12T16:00:00+08:00",
                  runner=runner)
    first, _ = collect_eastmoney(**kwargs)
    second, _ = collect_eastmoney(**kwargs)
    assert seen[0][1] == 900
    assert seen[0][0] != seen[1][0]
    assert first["batch_id"] != second["batch_id"]


def test_manifest_is_frozen_and_uses_explicit_targets_and_overlap_cursor(tmp_path: Path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"max_published_at": "2026-08-12T14:00:00+08:00",
                                  "max_content_id": "88"}))
    manifest_path = tmp_path / "batch" / "manifest.json"
    taxonomy = [{"sector_id": "robot", "sector_name": "机器人",
                 "eastmoney_forum_id": "bk0910"}]
    manifest = build_frozen_manifest(
        manifest_path, config=_config(), taxonomy=taxonomy,
        constituents={"robot": [{"stock_code": "000001", "turnover_20d": 10}]},
        trade_date="2026-08-12", collected_at="2026-08-12T16:00:00+08:00",
        output_file=tmp_path / "records.jsonl", cursor_file=cursor,
    )

    assert manifest["window_start"] == "2026-08-12T12:00:00+08:00"
    assert manifest["cursor"]["max_content_id"] == "88"
    assert [job["target_id"] for job in manifest["jobs"]] == ["bk0910", "000001"]
    assert manifest["jobs"][0]["url"] == "https://guba.eastmoney.com/list,bk0910.html"
    assert manifest["jobs"][0]["list_next_url_template"] == (
        "https://guba.eastmoney.com/list,bk0910_{page}.html"
    )
    assert manifest["jobs"][0]["window_start"] == manifest["window_start"]
    assert [job["posts_limit"] for job in manifest["jobs"]] == [100, 50]
    assert json.loads(manifest_path.read_text()) == manifest
    with pytest.raises(FileExistsError):
        build_frozen_manifest(
            manifest_path, config=_config(), taxonomy=taxonomy, constituents={},
            trade_date="2026-08-12", collected_at="2026-08-12T16:00:00+08:00",
            output_file=tmp_path / "other.jsonl", cursor_file=cursor,
        )


def test_grouped_coverage_requires_short_video_and_qualifying_eastmoney():
    good = {"status": "ok", "posts": 3, "independent_authors": 2,
            "parse_success_rate": 1.0}
    assert grouped_coverage([{"platform": "bili", "status": "ok"}], good, _config())["qualified"]
    assert not grouped_coverage([{"platform": "bili", "status": "failed"}], good, _config())["qualified"]
    assert not grouped_coverage([{"platform": "dy", "status": "ok"}],
                                {**good, "status": "blocked"}, _config())["qualified"]


def _record_shadow_history(store: ss.SentimentStore, qualified_days: int) -> None:
    """Seed hand-audited distinct shadow dates without involving live collection."""
    for offset in range(qualified_days):
        day = f"2026-07-{offset + 1:02d}"
        store.record_collection({
            "trade_date": day, "coverage": 0.0, "search_coverage": 0.0,
            "creator_coverage": 0.0, "platforms": {"eastmoney": {
                "phase": "shadow", "current_status": "ok", "shadow_qualified": True,
            }}, "funnel": {},
        })


def _promotion_config(tmp_path: Path, *, override_reason: str) -> dict:
    return {"sector_sentiment": {
        "enabled": True, "cache_dir": str(tmp_path), "platforms": ["eastmoney"],
        "taxonomy": [], "mediacrawler_commit": "a" * 40,
        "semantic_prompt_version": ss.PROMPT_VERSION,
        "retrieval": {"query_templates": ["{term} 股票"], "query_version": ss.QUERY_VERSION,
                      "relevance_version": ss.RELEVANCE_VERSION,
                      "creator_rule_version": ss.CREATOR_RULE_VERSION,
                      "max_contents_per_query": 2, "max_comments_per_content": 2},
        "eastmoney": {**_config(), "promotion_override_reason": override_reason},
    }}


def _empty_valid_eastmoney_runner(_manifest_path, report_path, *, timeout_seconds):
    Path(report_path).write_text(json.dumps({
        "parser_successes": 1, "parser_errors": 0,
        "request_successes": 1, "request_errors": 0,
    }))
    return CollectionResult("empty_valid", 0, 1, 0)


def test_promoted_eastmoney_rejects_thirteen_qualified_shadow_days(tmp_path: Path):
    """Catches promotion that is enabled before the required 14 qualified shadow dates."""
    store = ss.open_store(tmp_path)
    _record_shadow_history(store, 13)

    with pytest.raises(ValueError, match="14"):
        ss.run_configured(
            _promotion_config(tmp_path, override_reason=""), runner=lambda *_args: None,
            eastmoney_runner=_empty_valid_eastmoney_runner, trade_date="2026-08-12",
        )


def test_promoted_eastmoney_uses_fourteen_qualified_shadow_days_without_override(tmp_path: Path):
    """Catches a gate that ignores the 14th qualified shadow date or mis-audits its decision."""
    store = ss.open_store(tmp_path)
    _record_shadow_history(store, 14)

    result = ss.run_configured(
        _promotion_config(tmp_path, override_reason=""), runner=lambda *_args: None,
        eastmoney_runner=_empty_valid_eastmoney_runner, trade_date="2026-08-12",
    )

    audit = result["collection"]["eastmoney"]["promotion_audit"]
    assert audit == {
        "required_qualified_days": 14,
        "observed_qualified_days": 14,
        "eligible_by_history": True,
        "override_used": False,
        "override_reason": "",
    }
    assert ss.open_store(tmp_path).collection_summary("2026-08-12")["platforms"]["eastmoney"]["promotion_audit"] == audit


def test_reasoned_promotion_override_is_trimmed_and_persisted_in_collection_audit(tmp_path: Path):
    """Catches an unreasoned override or one that fails to leave its private audit trail."""
    result = ss.run_configured(
        _promotion_config(tmp_path, override_reason="  operator reviewed collector evidence  "),
        runner=lambda *_args: None, eastmoney_runner=_empty_valid_eastmoney_runner,
        trade_date="2026-08-12",
    )

    audit = result["collection"]["eastmoney"]["promotion_audit"]
    assert audit == {
        "required_qualified_days": 14,
        "observed_qualified_days": 0,
        "eligible_by_history": False,
        "override_used": True,
        "override_reason": "operator reviewed collector evidence",
    }
    persisted = ss.open_store(tmp_path).collection_summary("2026-08-12")
    assert persisted["platforms"]["eastmoney"]["promotion_audit"] == audit
    telemetry = json.loads(Path(result["collection"]["eastmoney"]["manifest_path"])
                           .with_name("telemetry.json").read_text())
    assert telemetry["promotion_audit"] == audit


def test_daily_score_coverage_is_per_sector_and_alert_is_suppressed(tmp_path: Path):
    store = ss.open_store(tmp_path)
    score = ss.build_daily_score(
        trade_date="2026-08-12", sector_id="robot", sector_name="机器人",
        taxonomy="concept", evidence=[{
            "platform": platform, "content_id": platform, "comment_id": "",
            "author_hash": f"{platform}-a", "text": "机器人看多", "stance": 1,
            "confidence": 1, "fomo": 1, "panic": 0, "mapping_confidence": 1,
        } for platform in ("bili", "dy")], history=[], market={},
    )
    store.save_daily_score(score)
    assert score["coverage_groups"] == {"short_video": True, "finance_community": False,
                                         "qualified": False}
    assert ss.advance_alerts(store, "2026-08-12")[0]["state"] == "insufficient_data"


def test_authoritative_coverage_quality_survives_sqlite_reload(tmp_path: Path):
    store = ss.open_store(tmp_path)
    score = ss.build_daily_score(
        trade_date="2026-08-12", sector_id="robot", sector_name="机器人",
        taxonomy="concept", evidence=[], history=[], market={},
        coverage_quality={"short_video": True, "finance_community": False,
                          "qualified": False, "eastmoney_status": "partial",
                          "eastmoney_posts": 1, "eastmoney_authors": 1,
                          "eastmoney_parse_success_rate": 1.0},
    )
    store.save_daily_score(score)
    loaded = ss.open_store(tmp_path).scores_for_date("2026-08-12")[0]
    assert loaded["coverage_quality"] == score["coverage_quality"]
    assert ss._coverage_ok(loaded) is False


def test_eastmoney_for_another_sector_does_not_satisfy_target_sector():
    robot = ss.build_daily_score(
        trade_date="2026-08-12", sector_id="robot", sector_name="机器人",
        taxonomy="concept", evidence=[{
            "platform": "bili", "content_id": "b1", "comment_id": "",
            "author_hash": "a", "text": "机器人看多", "stance": 1,
            "confidence": 1, "fomo": 0, "panic": 0, "mapping_confidence": 1,
        }], history=[], market={},
    )
    drug = ss.build_daily_score(
        trade_date="2026-08-12", sector_id="drug", sector_name="创新药",
        taxonomy="concept", evidence=[{
            "platform": "eastmoney", "content_id": "e1", "comment_id": "",
            "author_hash": "e", "text": "创新药看多", "stance": 1,
            "confidence": 1, "fomo": 0, "panic": 0, "mapping_confidence": 1,
        }], history=[], market={},
    )
    assert robot["coverage_groups"]["finance_community"] is False
    assert drug["coverage_groups"]["short_video"] is False


def test_eastmoney_volume_does_not_dominate_attention_or_consensus():
    common = {"comment_id": "", "confidence": 1, "fomo": 0, "panic": 0,
              "mapping_confidence": 1}
    evidence = [{**common, "platform": "bili", "content_id": "b", "author_hash": "b",
                 "text": "谨慎", "stance": -1}]
    evidence += [{**common, "platform": "eastmoney", "content_id": f"e{i}",
                  "author_hash": f"e{i}", "text": f"看多{i}", "stance": 1,
                  "source_type": "constituent_forum", "stock_code": "000001"}
                 for i in range(100)]
    score = ss.build_daily_score(trade_date="2026-08-12", sector_id="robot",
                                 sector_name="机器人", taxonomy="concept",
                                 evidence=evidence, history=[], market={})
    assert score["net_sentiment"] == pytest.approx(-0.35)
    assert score["content_count"] == pytest.approx(15.5)
    assert score["consensus_crowding"] < 0.3


def test_identical_constituent_comments_suppressed_by_cap_are_not_crowding_mass():
    rows = [{
        "platform": "eastmoney", "content_id": "p", "comment_id": f"c{i}",
        "author_hash": f"a{i}", "text": "全部梭哈", "stance": 1,
        "confidence": 1, "fomo": 1, "panic": 0, "mapping_confidence": 1,
        "source_type": "constituent_forum", "stock_code": "000001",
    } for i in range(100)]
    score = ss.build_daily_score(trade_date="2026-08-12", sector_id="robot",
                                 sector_name="机器人", taxonomy="concept",
                                 evidence=rows, history=[], market={})
    assert score["platform_contributions"]["eastmoney"]["diversity"] >= 0.70
    assert score["consensus_crowding"] < 0.35


def test_single_constituent_cannot_exceed_thirty_percent_of_eastmoney_signal():
    evidence = [{
        "platform": "eastmoney", "content_id": f"p{i}", "comment_id": "",
        "author_hash": f"a{i}", "text": "看多", "stance": 1.0, "confidence": 1.0,
        "fomo": 0.0, "panic": 0.0, "mapping_confidence": 1.0,
        "source_type": "constituent_forum", "stock_code": "000001",
    } for i in range(10)]
    score = ss.build_daily_score(
        trade_date="2026-08-12", sector_id="robot", sector_name="机器人",
        taxonomy="concept", evidence=evidence, history=[], market={},
    )
    contribution = score["platform_contributions"]["eastmoney"]
    assert contribution["net_sentiment"] == pytest.approx(0.3)
    assert contribution["constituent_stock_shares"]["000001"] == pytest.approx(0.3)
    assert score["content_count"] == pytest.approx(3.0)
    assert score["comment_count"] == 0


def test_eastmoney_explicit_target_mapping_does_not_require_alias_in_comment():
    evidence = ss._semantic_evidence({
        "platform": "eastmoney", "content_id": "p1", "comment_id": "c1",
        "text": "必须加仓，坚定看多", "sector_ids": ["robot"],
        "source_type": "sector_forum", "author_hash": "a1",
    }, [{"sector_id": "robot", "sector_name": "机器人", "taxonomy": "concept",
         "aliases": ["机器人"]}])
    assert len(evidence) == 1
    assert evidence[0]["mapping_confidence"] == pytest.approx(0.99)


def test_run_configured_keeps_short_video_when_eastmoney_fails(tmp_path: Path, monkeypatch):
    config = {
        "sector_sentiment": {
            "enabled": True, "cache_dir": str(tmp_path), "platforms": ["bili", "eastmoney"],
            "taxonomy": [{"sector_id": "robot", "sector_name": "机器人",
                          "taxonomy": "concept", "aliases": ["机器人"],
                          "eastmoney_forum_id": "bk0910"}],
            "mediacrawler_commit": "a" * 40,
            "semantic_prompt_version": ss.PROMPT_VERSION, "timeout_seconds": 3,
            "retrieval": {
                "query_templates": ["{term} 股票"], "query_version": ss.QUERY_VERSION,
                "relevance_version": ss.RELEVANCE_VERSION,
                "creator_rule_version": ss.CREATOR_RULE_VERSION,
                "max_contents_per_query": 2, "max_comments_per_content": 2,
            },
            "eastmoney": _config(),
        }
    }

    def media_runner(job, destination, timeout, limits):
        destination.write_text(json.dumps({
            "platform": "bili", "content_id": "b1", "text": "机器人股票看多",
            "published_at": "2026-08-12T15:00:00+08:00", "author_id": "u1",
        }) + "\n")

    def failed_eastmoney(*args, **kwargs):
        return CollectionResult("blocked", 0, 1, 1, message="blocked")

    provider_calls = []
    def constituent_provider(taxonomy, trade_date):
        provider_calls.append((taxonomy[0]["sector_id"], trade_date))
        return {"robot": [{"stock_code": "000001", "turnover_20d": 10}]}

    result = ss.run_configured(config, runner=media_runner, eastmoney_runner=failed_eastmoney,
                               eastmoney_constituent_provider=constituent_provider,
                               trade_date="2026-08-12")
    assert result["ingest"]["inserted"] == 1
    assert result["coverage"] == 0.0
    assert result["status"] == "degraded"
    assert result["collection"]["coverage_groups"]["finance_community"] is False
    assert provider_calls == [("robot", "2026-08-12")]


def test_shadow_eastmoney_does_not_change_scoring_coverage_or_policy_hash(tmp_path):
    config = {"sector_sentiment": {
        "enabled": True, "cache_dir": str(tmp_path / "shadow"), "platforms": ["bili", "dy"],
        "taxonomy": [{"sector_id": "robot", "sector_name": "机器人",
                      "taxonomy": "concept", "aliases": ["机器人"],
                      "eastmoney_forum_id": "bk0910"}],
        "mediacrawler_commit": "a" * 40, "semantic_prompt_version": ss.PROMPT_VERSION,
        "retrieval": {"query_templates": ["{term} 股票"],
                      "query_version": ss.QUERY_VERSION,
                      "relevance_version": ss.RELEVANCE_VERSION,
                      "creator_rule_version": ss.CREATOR_RULE_VERSION,
                      "max_contents_per_query": 20, "max_comments_per_content": 20},
        "eastmoney": {**_config(), "phase": "shadow"},
    }}
    def media(job, destination, timeout, limits):
        destination.write_text("".join(json.dumps({"platform": job.platform, "content_id": f"{job.platform}-{i}",
            "text": "机器人股票必须起飞坚定看多", "title": "机器人板块股票资金流入",
            "published_at": "2026-08-12T10:00:00+08:00", "author_id": f"{job.platform}-{i}",
            "author_hash": f"{job.platform}-{i}"},
            ensure_ascii=False) + "\n" for i in range(12)))
    def east(manifest_path, report_path, *, timeout_seconds):
        manifest = json.loads(Path(manifest_path).read_text())
        Path(manifest["output_file"]).write_text(json.dumps({"platform": "eastmoney",
            "content_id": "e1", "comment_id": "", "text": "机器人股票必须起飞",
            "published_at": "2026-08-12T10:00:00+08:00", "collected_at": manifest["collected_at"],
            "trade_date": "2026-08-12", "batch_id": manifest["batch_id"], "author_hash": "e1",
            "sector_ids": ["robot"], "source_type": "sector_forum"}, ensure_ascii=False) + "\n")
        Path(report_path).write_text(json.dumps({"parser_successes": 1, "parser_errors": 0,
                                                "request_successes": 1, "request_errors": 0}))
        return CollectionResult("ok", 1, 1, 0)
    baseline = copy.deepcopy(config)
    baseline["sector_sentiment"]["cache_dir"] = str(tmp_path / "baseline")
    baseline["sector_sentiment"].pop("eastmoney")
    baseline_result = ss.run_configured(baseline, runner=media, trade_date="2026-08-12")
    result = ss.run_configured(config, runner=media, eastmoney_runner=east,
                               trade_date="2026-08-12")
    assert result["ingest"]["inserted"] == 25, result
    baseline_store = ss.open_store(tmp_path / "baseline")
    shadow_store = ss.open_store(tmp_path / "shadow")
    score = shadow_store.scores_for_date("2026-08-12")[0]
    baseline_score = baseline_store.scores_for_date("2026-08-12")[0]
    print(baseline_score)
    assert score == baseline_score
    baseline_state = baseline_store.get_state("robot")
    shadow_state = shadow_store.get_state("robot")
    # event_id / event_key / timestamps are per-run; everything else must match.
    volatile = ("event_id", "event_key", "opened_at", "updated_at", "created_at")
    assert ({k: v for k, v in shadow_state.items() if k not in volatile}
            == {k: v for k, v in baseline_state.items() if k not in volatile})
    assert baseline_state["state"] == "observe"
    baseline_events = baseline_store.list_events()
    shadow_events = shadow_store.list_events()
    assert ([{k: v for k, v in event.items() if k not in volatile} for event in shadow_events]
            == [{k: v for k, v in event.items() if k not in volatile} for event in baseline_events])
    assert len(baseline_events) == 1
    assert {key: baseline_events[0][key] for key in (
        "sector_id", "sector_name", "taxonomy", "event_type", "trade_date",
    )} == {"sector_id": "robot", "sector_name": "机器人", "taxonomy": "concept",
          "event_type": "opened", "trade_date": "2026-08-12"}
    assert result["coverage"] == baseline_result["coverage"]
    assert result["coverage"] == 1.0
    assert result["collection"]["eastmoney"]["phase"] == "shadow"
    assert score["platforms"] == ["bili", "dy"]
    original_hash = result["collection"]["config_hash"]
    config["sector_sentiment"]["eastmoney"]["max_requests"] = 499
    assert ss._validate_retrieval_policy(config["sector_sentiment"],
                                         config["sector_sentiment"]["retrieval"]) == original_hash
    config["sector_sentiment"]["eastmoney"]["phase"] = "promoted"
    assert ss._validate_retrieval_policy(config["sector_sentiment"],
                                         config["sector_sentiment"]["retrieval"]) != original_hash


def test_pre_feature_policy_hash_is_golden_and_shadow_is_byte_compatible(tmp_path):
    settings = {
        "platforms": ["bili", "dy"], "mediacrawler_commit": "a" * 40,
        "semantic_prompt_version": ss.PROMPT_VERSION,
        "taxonomy": [{"sector_id": "robot", "sector_name": "机器人",
                      "taxonomy": "concept", "aliases": ["机器人"]}],
    }
    retrieval = {"query_version": ss.QUERY_VERSION,
                 "relevance_version": ss.RELEVANCE_VERSION,
                 "creator_rule_version": ss.CREATOR_RULE_VERSION,
                 "query_templates": ["{term} 股票"],
                 "max_contents_per_query": 20, "max_comments_per_content": 20}
    legacy_hash = ss._validate_retrieval_policy(settings, retrieval)
    shadow = copy.deepcopy(settings)
    shadow["eastmoney"] = {**_config(), "phase": "shadow"}
    assert ss._validate_retrieval_policy(shadow, retrieval) == legacy_hash
    assert legacy_hash == "3255d32e06ab4f6857920e782a448423a4f36e8b8f713b7689f99b407c59cde6"


def test_eastmoney_forum_id_is_excluded_from_shadow_but_frozen_when_promoted():
    settings = {
        "platforms": ["bili", "dy"], "mediacrawler_commit": "a" * 40,
        "semantic_prompt_version": ss.PROMPT_VERSION,
        "taxonomy": [{"sector_id": "robot", "sector_name": "机器人",
                      "taxonomy": "concept", "aliases": ["机器人"]}],
    }
    retrieval = {"query_version": ss.QUERY_VERSION,
                 "relevance_version": ss.RELEVANCE_VERSION,
                 "creator_rule_version": ss.CREATOR_RULE_VERSION,
                 "query_templates": ["{term} 股票"],
                 "max_contents_per_query": 20, "max_comments_per_content": 20}
    legacy_hash = ss._validate_retrieval_policy(settings, retrieval)
    shadow = copy.deepcopy(settings)
    shadow["eastmoney"] = {**_config(), "phase": "shadow"}
    shadow["taxonomy"][0]["eastmoney_forum_id"] = "bk0910"
    assert ss._validate_retrieval_policy(shadow, retrieval) == legacy_hash

    promoted = copy.deepcopy(shadow)
    promoted["eastmoney"]["phase"] = "promoted"
    first_promoted_hash = ss._validate_retrieval_policy(promoted, retrieval)
    promoted["taxonomy"][0]["eastmoney_forum_id"] = "bk1036"
    assert ss._validate_retrieval_policy(promoted, retrieval) != first_promoted_hash


def test_real_batch_jsonl_to_score_state_replay_is_idempotent(tmp_path: Path):
    config = {"sector_sentiment": {
        "enabled": True, "cache_dir": str(tmp_path), "platforms": ["bili", "eastmoney"],
        "taxonomy": [{"sector_id": "robot", "sector_name": "机器人",
                      "taxonomy": "concept", "aliases": ["机器人"],
                      "eastmoney_forum_id": "bk0910"}],
        "mediacrawler_commit": "a" * 40, "semantic_prompt_version": ss.PROMPT_VERSION,
        "retrieval": {"query_templates": ["{term} 股票"],
                      "query_version": ss.QUERY_VERSION,
                      "relevance_version": ss.RELEVANCE_VERSION,
                      "creator_rule_version": ss.CREATOR_RULE_VERSION,
                      "max_contents_per_query": 20, "max_comments_per_content": 20},
        "eastmoney": _config(),
    }}
    constituents = [
        {"stock_code": f"00000{index}", "turnover_20d": float(600 - index * 100),
         "turnover_as_of": "2026-08-12",
         "membership_source": "replay-fixture-members-v1",
         "turnover_source": "replay-fixture-turnover-v1"}
        for index in range(1, 6)
    ]
    representative_provenance = {"robot": {
        "sector_id": "robot", "membership_source": "replay-fixture-members-v1",
        "turnover_source": "replay-fixture-turnover-v1", "turnover_as_of": "2026-08-12",
        "lookback_trading_days": 20, "requested_count": 5, "selected_count": 5,
        "selected": constituents, "status": "ok",
    }}

    def constituent_provider(taxonomy, trade_date):
        assert taxonomy == config["sector_sentiment"]["taxonomy"]
        assert trade_date == "2026-08-12"
        return {"robot": constituents}, representative_provenance

    def media(job, destination, timeout, limits):
        destination.write_text("".join(json.dumps({
            "platform": "bili", "content_id": f"b{i}", "text": "机器人股票资金流入必须起飞坚定看多",
            "title": "机器人板块股票资金流入",
            "published_at": "2026-08-12T15:00:00+08:00", "author_id": f"b{i}",
        }, ensure_ascii=False) + "\n" for i in range(12)))
    def east(manifest_path, report_path, *, timeout_seconds):
        manifest = json.loads(Path(manifest_path).read_text())
        output = Path(manifest["output_file"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("".join(json.dumps({
            "platform": "eastmoney", "content_id": f"e{i}", "comment_id": "",
            "text": "机器人股票资金流入必须起飞坚定看多", "published_at": "2026-08-12T15:00:00+08:00",
            "title": "机器人板块股票资金流入",
            "collected_at": manifest["collected_at"], "trade_date": "2026-08-12",
            "batch_id": manifest["batch_id"], "author_hash": f"e{i}",
            "sector_ids": ["robot"], "source_type": "sector_forum",
        }, ensure_ascii=False) + "\n" for i in range(12)))
        Path(report_path).write_text(json.dumps({"parser_successes": 1, "parser_errors": 0,
                                                "request_successes": 1, "request_errors": 0}))
        return CollectionResult("ok", 12, 1, 0)
    first_result = ss.run_configured(
        config, runner=media, eastmoney_runner=east,
        eastmoney_constituent_provider=constituent_provider, trade_date="2026-08-12",
    )
    assert first_result["ingest"]["inserted"] == 24
    store = ss.open_store(tmp_path)
    assert len(store.list_eligible_content("2026-08-12")) == 24
    first_score = store.scores_for_date("2026-08-12")
    first_state = store.get_state("robot")
    first_events = store.list_events()
    ss.run_configured(
        config, runner=media, eastmoney_runner=east,
        eastmoney_constituent_provider=constituent_provider, trade_date="2026-08-12",
    )
    assert store.scores_for_date("2026-08-12") == first_score
    assert store.get_state("robot") == first_state
    assert store.list_events() == first_events
    assert first_score[0]["coverage_quality"]["qualified"] is True
    assert first_state["state"] == "observe" and len(first_events) == 1


def test_representative_provider_selects_deterministic_top_five_from_twenty_day_turnover():
    """Catches a provider that requests the wrong lookback or leaves ties non-deterministic."""
    from apex.eastmoney_guba.representatives import select_representative_constituents

    membership_calls = []
    turnover_calls = []
    taxonomy = [{"sector_id": "robot", "sector_name": "机器人",
                 "eastmoney_forum_id": "bk0910"}]

    def membership_fetcher(sector):
        membership_calls.append(sector)
        return ([
            {"stock_code": "000006"}, {"stock_code": "000004"},
            {"stock_code": "000002"}, {"stock_code": "000001"},
            {"stock_code": "000003"}, {"stock_code": "000005"},
        ], "eastmoney-sector-members")

    def turnover_fetcher(stock_codes, trade_date, trading_days):
        turnover_calls.append((stock_codes, trade_date, trading_days))
        return ({
            "000001": 200.0, "000002": 350.0, "000003": 350.0,
            "000004": 410.0, "000005": 100.0, "000006": 520.0,
        }, "2026-08-12", "eastmoney-daily-turnover")

    constituents, provenance = select_representative_constituents(
        taxonomy, "2026-08-13", membership_fetcher=membership_fetcher,
        turnover_fetcher=turnover_fetcher,
    )

    assert membership_calls == taxonomy
    assert turnover_calls == [(
        ["000006", "000004", "000002", "000001", "000003", "000005"],
        "2026-08-13", 20,
    )]
    assert constituents == {"robot": [
        {"stock_code": "000006", "turnover_20d": 520.0, "turnover_as_of": "2026-08-12",
         "membership_source": "eastmoney-sector-members", "turnover_source": "eastmoney-daily-turnover"},
        {"stock_code": "000004", "turnover_20d": 410.0, "turnover_as_of": "2026-08-12",
         "membership_source": "eastmoney-sector-members", "turnover_source": "eastmoney-daily-turnover"},
        {"stock_code": "000002", "turnover_20d": 350.0, "turnover_as_of": "2026-08-12",
         "membership_source": "eastmoney-sector-members", "turnover_source": "eastmoney-daily-turnover"},
        {"stock_code": "000003", "turnover_20d": 350.0, "turnover_as_of": "2026-08-12",
         "membership_source": "eastmoney-sector-members", "turnover_source": "eastmoney-daily-turnover"},
        {"stock_code": "000001", "turnover_20d": 200.0, "turnover_as_of": "2026-08-12",
         "membership_source": "eastmoney-sector-members", "turnover_source": "eastmoney-daily-turnover"},
    ]}
    assert provenance == {"robot": {
        "sector_id": "robot",
        "membership_source": "eastmoney-sector-members",
        "turnover_source": "eastmoney-daily-turnover",
        "turnover_as_of": "2026-08-12",
        "lookback_trading_days": 20,
        "requested_count": 5,
        "selected_count": 5,
        "selected": constituents["robot"],
        "status": "ok",
    }}


def test_default_membership_provider_uses_explicit_forum_code_and_selects_five_representatives(
    monkeypatch,
):
    """Catches AkShare's name-map IndexError when the configured board code is available."""
    import sys
    import types

    import pandas as pd

    from apex.eastmoney_guba import representatives

    requested_symbols = []

    def concept_members(*, symbol: str):
        requested_symbols.append(symbol)
        return pd.DataFrame({"代码": ["000001", "000002", "000003", "000004", "000005"]})

    monkeypatch.setitem(sys.modules, "akshare", types.SimpleNamespace(
        stock_board_concept_cons_em=concept_members,
    ))

    constituents, provenance = representatives.select_representative_constituents(
        [{"sector_id": "robot", "sector_name": "机器人", "taxonomy": "concept",
          "eastmoney_forum_id": "bk1408"}],
        "2026-08-17",
        turnover_fetcher=lambda codes, _trade_date, days: (
            {code: float(index) for index, code in enumerate(codes, start=1)},
            "2026-08-17", f"fixture.daily.{days}",
        ),
    )

    assert requested_symbols == ["BK1408"]
    assert [row["stock_code"] for row in constituents["robot"]] == [
        "000005", "000004", "000003", "000002", "000001",
    ]
    assert provenance["robot"]["status"] == "ok"
    assert provenance["robot"]["selected_count"] == 5


def test_manifest_normalizes_nonempty_injected_constituents_into_provenance(tmp_path: Path):
    """Catches direct manifests that silently freeze no provenance for supplied targets."""
    selected = [{"stock_code": f"00000{index}", "turnover_20d": float(10 - index),
                 "turnover_as_of": "2026-08-12"} for index in range(1, 6)]
    manifest = build_frozen_manifest(
        tmp_path / "batch" / "manifest.json", config=_config(),
        taxonomy=[{"sector_id": "robot", "sector_name": "机器人", "eastmoney_forum_id": "bk0910"}],
        constituents={"robot": selected}, trade_date="2026-08-13",
        collected_at="2026-08-13T16:00:00+08:00", output_file=tmp_path / "records.jsonl",
        cursor_file=tmp_path / "cursor.json",
    )

    provenance = manifest["representative_constituents"]["robot"]
    assert provenance["status"] == "ok"
    assert provenance["membership_source"] == "injected"
    assert provenance["turnover_source"] == "injected.turnover_20d"
    assert all(row["membership_source"] == "injected" for row in provenance["selected"])
    assert all(row["turnover_source"] == "injected.turnover_20d" for row in provenance["selected"])


def test_manifest_freezes_representative_provenance_and_target_shortfall(tmp_path: Path):
    """Catches a manifest that silently schedules only a sector forum after a shortfall."""
    selected = [
        {"stock_code": "000001", "turnover_20d": 300.0, "turnover_as_of": "2026-08-12",
         "membership_source": "sector-members-v1", "turnover_source": "daily-turnover-v1"},
        {"stock_code": "000002", "turnover_20d": 200.0, "turnover_as_of": "2026-08-12",
         "membership_source": "sector-members-v1", "turnover_source": "daily-turnover-v1"},
    ]
    provenance = {"robot": {
        "sector_id": "robot", "membership_source": "sector-members-v1",
        "turnover_source": "daily-turnover-v1", "turnover_as_of": "2026-08-12",
        "lookback_trading_days": 20, "requested_count": 5, "selected_count": 2,
        "selected": selected, "status": "insufficient",
        "reason": "only 2 valid constituents have twenty-day turnover",
    }}
    manifest = build_frozen_manifest(
        tmp_path / "batch" / "manifest.json", config=_config(),
        taxonomy=[{"sector_id": "robot", "sector_name": "机器人",
                   "eastmoney_forum_id": "bk0910"}],
        constituents={"robot": selected}, representative_provenance=provenance,
        trade_date="2026-08-13", collected_at="2026-08-13T16:00:00+08:00",
        output_file=tmp_path / "batch" / "records.jsonl", cursor_file=tmp_path / "cursor.json",
    )

    assert manifest["representative_constituents"] == provenance
    assert {"sector_id": "robot", "reason": "target_shortfall",
            "requested_count": 5, "selected_count": 2} in manifest["missing_targets"]


def test_run_configured_uses_default_representative_provider_and_freezes_its_provenance(
    tmp_path: Path, monkeypatch,
):
    """Catches the legacy static-constituents fallback in the normal configured path."""
    selected = [{"stock_code": "000001", "turnover_20d": 300.0,
                 "turnover_as_of": "2026-08-12", "membership_source": "members-v1",
                 "turnover_source": "turnover-v1"}]
    provenance = {"robot": {
        "sector_id": "robot", "membership_source": "members-v1",
        "turnover_source": "turnover-v1", "turnover_as_of": "2026-08-12",
        "lookback_trading_days": 20, "requested_count": 5, "selected_count": 1,
        "selected": selected, "status": "insufficient", "reason": "fixture shortfall",
    }}
    calls = []

    def selector(taxonomy, trade_date, constituent_count=5):
        calls.append((taxonomy, trade_date, constituent_count))
        return {"robot": selected}, provenance

    monkeypatch.setattr(
        "apex.eastmoney_guba.representatives.select_representative_constituents", selector,
    )
    frozen = {}

    def eastmoney_runner(manifest_path, report_path, *, timeout_seconds):
        frozen.update(json.loads(Path(manifest_path).read_text()))
        Path(report_path).write_text(json.dumps({
            "parser_successes": 1, "parser_errors": 0,
            "request_successes": 1, "request_errors": 0,
        }))
        return CollectionResult("empty_valid", 0, 1, 0)

    config = {"sector_sentiment": {
        "enabled": True, "cache_dir": str(tmp_path), "platforms": ["eastmoney"],
        "taxonomy": [{"sector_id": "robot", "sector_name": "机器人", "taxonomy": "concept",
                      "aliases": ["机器人"], "eastmoney_forum_id": "bk0910"}],
        "mediacrawler_commit": "a" * 40, "semantic_prompt_version": ss.PROMPT_VERSION,
        "retrieval": {"query_templates": ["{term} 股票"], "query_version": ss.QUERY_VERSION,
                      "relevance_version": ss.RELEVANCE_VERSION,
                      "creator_rule_version": ss.CREATOR_RULE_VERSION,
                      "max_contents_per_query": 2, "max_comments_per_content": 2},
        "eastmoney": _config(),
    }}
    result = ss.run_configured(config, runner=lambda *_args: None,
                               eastmoney_runner=eastmoney_runner, trade_date="2026-08-13")

    assert calls == [(config["sector_sentiment"]["taxonomy"], "2026-08-13", 5)]
    assert frozen["representative_constituents"] == provenance
    assert result["collection"]["eastmoney"]["shadow_qualified"] is False


def test_short_representative_provider_result_surfaces_target_shortfall_and_degrades(
    tmp_path: Path,
):
    """Catches a successful sector-only collection being incorrectly quality-qualified."""
    selected = [{"stock_code": "000001", "turnover_20d": 300.0,
                 "turnover_as_of": "2026-08-12", "membership_source": "members-v1",
                 "turnover_source": "turnover-v1"}]
    provenance = {"robot": {
        "sector_id": "robot", "membership_source": "members-v1",
        "turnover_source": "turnover-v1", "turnover_as_of": "2026-08-12",
        "lookback_trading_days": 20, "requested_count": 5, "selected_count": 1,
        "selected": selected, "status": "insufficient", "reason": "fixture shortfall",
    }}

    def eastmoney_runner(manifest_path, report_path, *, timeout_seconds):
        Path(report_path).write_text(json.dumps({
            "parser_successes": 1, "parser_errors": 0,
            "request_successes": 1, "request_errors": 0,
        }))
        return CollectionResult("ok", 0, 1, 0)

    config = {"sector_sentiment": {
        "enabled": True, "cache_dir": str(tmp_path), "platforms": ["eastmoney"],
        "taxonomy": [{"sector_id": "robot", "sector_name": "机器人", "taxonomy": "concept",
                      "aliases": ["机器人"], "eastmoney_forum_id": "bk0910"}],
        "mediacrawler_commit": "a" * 40, "semantic_prompt_version": ss.PROMPT_VERSION,
        "retrieval": {"query_templates": ["{term} 股票"], "query_version": ss.QUERY_VERSION,
                      "relevance_version": ss.RELEVANCE_VERSION,
                      "creator_rule_version": ss.CREATOR_RULE_VERSION,
                      "max_contents_per_query": 2, "max_comments_per_content": 2},
        "eastmoney": _config(),
    }}
    result = ss.run_configured(
        config, runner=lambda *_args: None, eastmoney_runner=eastmoney_runner,
        eastmoney_constituent_provider=lambda *_args: ({"robot": selected}, provenance),
        trade_date="2026-08-13",
    )

    eastmoney = result["collection"]["eastmoney"]
    assert result["status"] == "degraded"
    assert eastmoney["status"] == "degraded"
    assert eastmoney["target_shortfall"] is True
    assert eastmoney["shadow_qualified"] is False


def test_representative_provider_failure_surfaces_target_shortfall_and_degrades(tmp_path: Path):
    """Catches provider exceptions being hidden behind a seemingly healthy sector forum."""
    config = {"sector_sentiment": {
        "enabled": True, "cache_dir": str(tmp_path), "platforms": ["eastmoney"],
        "taxonomy": [{"sector_id": "robot", "sector_name": "机器人", "taxonomy": "concept",
                      "aliases": ["机器人"], "eastmoney_forum_id": "bk0910"}],
        "mediacrawler_commit": "a" * 40, "semantic_prompt_version": ss.PROMPT_VERSION,
        "retrieval": {"query_templates": ["{term} 股票"], "query_version": ss.QUERY_VERSION,
                      "relevance_version": ss.RELEVANCE_VERSION,
                      "creator_rule_version": ss.CREATOR_RULE_VERSION,
                      "max_contents_per_query": 2, "max_comments_per_content": 2},
        "eastmoney": _config(),
    }}

    def failed_provider(*_args):
        raise RuntimeError("turnover source unavailable")

    result = ss.run_configured(
        config, runner=lambda *_args: None,
        eastmoney_constituent_provider=failed_provider, trade_date="2026-08-13",
    )

    eastmoney = result["collection"]["eastmoney"]
    assert result["status"] == "degraded"
    assert eastmoney["status"] == "degraded"
    assert eastmoney["target_shortfall"] is True
    assert eastmoney["shadow_qualified"] is False


def test_provider_proxy_unavailability_has_a_safe_top_level_reason_and_audited_shortfall(
    tmp_path: Path,
):
    """Catches a visible target shortfall whose telemetry gives users no reason to act."""
    provenance = {"robot": {
        "sector_id": "robot", "membership_source": "unavailable",
        "turnover_source": "unavailable", "turnover_as_of": "2026-08-17",
        "lookback_trading_days": 20, "requested_count": 5, "selected_count": 0,
        "selected": [], "status": "failed",
        "reason": "representative provider failed: ProxyError",
    }}

    def runner(_manifest_path, report_path, *, timeout_seconds):
        Path(report_path).write_text(json.dumps({
            "parser_successes": 1, "parser_errors": 0,
            "request_successes": 1, "request_errors": 0,
        }))
        return CollectionResult("empty_valid", 0, 1, 0)

    telemetry, _ = collect_eastmoney(
        cache_dir=tmp_path, config=_config(),
        taxonomy=[{"sector_id": "robot", "sector_name": "\xe6\x9c\xba\xe5\x99\xa8\xe4\xba\xba",
                   "eastmoney_forum_id": "bk1408"}],
        constituents={"robot": []}, representative_provenance=provenance,
        trade_date="2026-08-17", collected_at="2026-08-17T23:10:00+08:00", runner=runner,
    )

    assert telemetry["status"] == "degraded"
    assert telemetry["target_shortfall"] is True
    assert telemetry["message"] == "representative_provider_unavailable"
    assert telemetry["representative_constituents"]["robot"]["reason"] == (
        "representative provider failed: ProxyError"
    )


def test_tuple_provider_normalizes_missing_and_inconsistent_sector_provenance(tmp_path: Path):
    """Catches tuple injection that silently permits a sector-only qualified run."""
    taxonomy = [
        {"sector_id": "robot", "sector_name": "机器人", "taxonomy": "concept",
         "aliases": ["机器人"], "eastmoney_forum_id": "bk0910"},
        {"sector_id": "chip", "sector_name": "芯片", "taxonomy": "concept",
         "aliases": ["芯片"], "eastmoney_forum_id": "bk1036"},
    ]
    config = {"sector_sentiment": {
        "enabled": True, "cache_dir": str(tmp_path), "platforms": ["eastmoney"],
        "taxonomy": taxonomy, "mediacrawler_commit": "a" * 40,
        "semantic_prompt_version": ss.PROMPT_VERSION,
        "retrieval": {"query_templates": ["{term} 股票"], "query_version": ss.QUERY_VERSION,
                      "relevance_version": ss.RELEVANCE_VERSION,
                      "creator_rule_version": ss.CREATOR_RULE_VERSION,
                      "max_contents_per_query": 2, "max_comments_per_content": 2},
        "eastmoney": _config(),
    }}
    selected = [{"stock_code": "000001", "turnover_20d": 300.0,
                 "turnover_as_of": "2026-08-12", "membership_source": "members-v1",
                 "turnover_source": "turnover-v1"}]
    claimed_ok = {"robot": {
        "sector_id": "robot", "membership_source": "members-v1",
        "turnover_source": "turnover-v1", "turnover_as_of": "2026-08-12",
        "lookback_trading_days": 20, "requested_count": 5, "selected_count": 5,
        "selected": [dict(selected[0]) for _ in range(5)], "status": "ok",
    }}
    frozen = {}

    def eastmoney_runner(manifest_path, report_path, *, timeout_seconds):
        frozen.update(json.loads(Path(manifest_path).read_text()))
        Path(report_path).write_text(json.dumps({
            "parser_successes": 1, "parser_errors": 0,
            "request_successes": 1, "request_errors": 0,
        }))
        return CollectionResult("ok", 0, 1, 0)

    result = ss.run_configured(
        config, runner=lambda *_args: None, eastmoney_runner=eastmoney_runner,
        eastmoney_constituent_provider=lambda *_args: (
            {"robot": selected, "chip": []}, claimed_ok,
        ),
        trade_date="2026-08-13",
    )

    representative = frozen["representative_constituents"]
    assert representative["robot"]["selected"] == selected
    assert representative["robot"]["selected_count"] == 1
    assert representative["robot"]["status"] == "insufficient"
    assert representative["chip"]["selected"] == []
    assert representative["chip"]["selected_count"] == 0
    assert representative["chip"]["status"] == "failed"
    assert {item["sector_id"] for item in frozen["missing_targets"]
            if item["reason"] == "target_shortfall"} == {"robot", "chip"}
    assert result["status"] == "degraded"
    assert result["collection"]["eastmoney"]["shadow_qualified"] is False


def test_provider_failure_writes_safe_degraded_audit_without_live_runner(tmp_path: Path):
    """Catches a failed provider that skips the frozen audit or stores its raw exception."""
    secret = "https://token:super-secret@example.invalid/private/path"
    config = {"sector_sentiment": {
        "enabled": True, "cache_dir": str(tmp_path), "platforms": ["eastmoney"],
        "taxonomy": [{"sector_id": "robot", "sector_name": "机器人", "taxonomy": "concept",
                      "aliases": ["机器人"], "eastmoney_forum_id": "bk0910"}],
        "mediacrawler_commit": "a" * 40, "semantic_prompt_version": ss.PROMPT_VERSION,
        "retrieval": {"query_templates": ["{term} 股票"], "query_version": ss.QUERY_VERSION,
                      "relevance_version": ss.RELEVANCE_VERSION,
                      "creator_rule_version": ss.CREATOR_RULE_VERSION,
                      "max_contents_per_query": 2, "max_comments_per_content": 2},
        "eastmoney": _config(),
    }}

    def failed_provider(*_args):
        raise RuntimeError(secret)

    def live_runner(*_args, **_kwargs):
        raise AssertionError("provider failure must not launch live sector-only scraping")

    result = ss.run_configured(
        config, runner=lambda *_args: None, eastmoney_runner=live_runner,
        eastmoney_constituent_provider=failed_provider, trade_date="2026-08-13",
    )

    eastmoney = result["collection"]["eastmoney"]
    manifest = json.loads(Path(eastmoney["manifest_path"]).read_text())
    persisted = "\n".join([
        json.dumps(eastmoney, ensure_ascii=False),
        Path(eastmoney["manifest_path"]).read_text(),
        Path(eastmoney["manifest_path"]).with_name("telemetry.json").read_text(),
        json.dumps(ss.open_store(tmp_path).collection_summary("2026-08-13"), ensure_ascii=False),
    ])
    assert eastmoney["status"] == "degraded"
    assert eastmoney["target_shortfall"] is True
    assert eastmoney["shadow_qualified"] is False
    assert all(item["status"] == "failed"
               for item in manifest["representative_constituents"].values())
    assert all(item["reason"] == "representative_provider_unavailable"
               for item in manifest["representative_constituents"].values())
    assert secret not in persisted
    assert "RuntimeError" not in persisted


def test_collection_telemetry_replaces_runner_and_report_exception_text(tmp_path: Path):
    """Catches raw collector exception details being persisted in Eastmoney telemetry."""
    secret = "https://token:super-secret@example.invalid/private/path"

    def runner(_manifest_path, report_path, *, timeout_seconds):
        Path(report_path).write_text(json.dumps({
            "parser_successes": 0, "parser_errors": 1,
            "request_successes": 0, "request_errors": 1, "message": secret,
        }))
        return CollectionResult("failed", 0, 1, 1, message=secret)

    telemetry, _ = collect_eastmoney(
        cache_dir=tmp_path, config=_config(), taxonomy=[], constituents={},
        trade_date="2026-08-13", collected_at="2026-08-13T16:00:00+08:00", runner=runner,
    )

    persisted = "\n".join([
        json.dumps(telemetry, ensure_ascii=False),
        Path(telemetry["manifest_path"]).with_name("telemetry.json").read_text(),
    ])
    assert telemetry["message"] == "collector_failed"
    assert secret not in persisted
