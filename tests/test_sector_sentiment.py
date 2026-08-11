import json
import os
import sys
from datetime import date

import pytest

from apex import sector_sentiment as ss
from apex import sector_retrieval as retrieval


def _item(platform: str, content_id: str, text: str, **extra) -> dict:
    return {
        "platform": platform,
        "content_id": content_id,
        "comment_id": extra.pop("comment_id", ""),
        "published_at": "2026-08-10T12:00:00+08:00",
        "collected_at": "2026-08-10T18:30:00+08:00",
        "text": text,
        "engagement": 10,
        "url": f"https://example.test/{content_id}",
        "batch_id": "2026-08-10-bili-1",
        **extra,
    }


def test_ingest_is_idempotent_and_cross_platform_reposts_are_downweighted(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    first = store.ingest([
        _item("bili", "a", "机器人板块继续看多，坚定持有"),
        _item("eastmoney", "b", "机器人板块继续看多，坚定持有"),
    ])
    second = store.ingest([_item("bili", "a", "机器人板块继续看多，坚定持有")])

    assert first == {"inserted": 2, "duplicates": 0}
    assert second == {"inserted": 0, "duplicates": 1}
    rows = store.list_content("2026-08-10")
    assert len(rows) == 2
    assert sorted(row["repost_weight"] for row in rows) == [0.35, 1.0]


def test_aggregate_requires_two_platforms_before_opening_observation(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.save_daily_score({
        "trade_date": "2026-08-10", "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "platforms": ["bili"], "independent_authors": 50,
        "mapping_confidence": 0.95, "sentiment_extreme": 0.96,
        "attention_acceleration": 0.97, "consensus_crowding": 0.91,
        "market_divergence": 0.20, "short_risk": 80, "swing_risk": 55,
    })

    result = ss.advance_alerts(store, "2026-08-10")

    assert result[0]["state"] == "insufficient_data"
    assert store.list_events() == []


def test_alert_state_moves_observe_warn_then_resolves_without_duplicate_events(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    base = {
        "sector_id": "concept:robot", "sector_name": "机器人", "taxonomy": "concept",
        "platforms": ["bili", "eastmoney"], "independent_authors": 20,
        "mapping_confidence": 0.9, "sentiment_extreme": 0.95,
        "attention_acceleration": 0.92, "consensus_crowding": 0.80,
        "market_divergence": 0.20, "short_risk": 66, "swing_risk": 48,
    }
    store.save_daily_score({**base, "trade_date": "2026-08-10"})
    assert ss.advance_alerts(store, "2026-08-10")[0]["state"] == "observe"

    store.save_daily_score({**base, "trade_date": "2026-08-11", "consensus_crowding": 0.93,
                            "market_divergence": 0.75, "short_risk": 88})
    assert ss.advance_alerts(store, "2026-08-11")[0]["state"] == "warning"
    assert ss.advance_alerts(store, "2026-08-11")[0]["changed"] is False

    cool = {**base, "sentiment_extreme": 0.50, "attention_acceleration": 0.40,
            "consensus_crowding": 0.40, "market_divergence": 0.10}
    for day in ("2026-08-12", "2026-08-13", "2026-08-14"):
        store.save_daily_score({**cool, "trade_date": day})
        last = ss.advance_alerts(store, day)[0]
    assert last["state"] == "resolved"
    events = store.list_events()
    assert [event["event_type"] for event in events] == ["opened", "upgraded", "resolved"]
    assert len({event["event_id"] for event in events}) == 1


def test_outcome_labels_start_after_signal_date_and_use_trading_dates(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.create_alert("concept:robot", "机器人", "concept", "2026-08-07", "warning")
    prices = [
        {"trade_date": "2026-08-07", "sector_return": -10.0, "benchmark_return": 0.0},
        {"trade_date": "2026-08-10", "sector_return": -1.0, "benchmark_return": 0.0},
        {"trade_date": "2026-08-11", "sector_return": -2.2, "benchmark_return": 0.0},
        {"trade_date": "2026-08-12", "sector_return": 0.1, "benchmark_return": 0.0},
    ]

    label = ss.label_event(store, "concept:robot", "2026-08-07", prices)

    assert label["short_hit"] is True
    assert label["short_max_drawdown"] == pytest.approx(-3.2)


def test_historical_event_can_be_labeled_after_sector_reopens(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    first = store.create_alert("concept:robot", "机器人", "concept", "2026-08-01", "warning")
    store.create_alert("concept:robot", "机器人", "concept", "2026-08-10", "warning")

    result = ss.label_event(store, "concept:robot", "2026-08-01", [
        {"trade_date": "2026-08-04", "sector_return": -3.0, "benchmark_return": 0.0},
    ])

    assert result["event_id"] == first["event_id"]


def test_external_collector_failure_is_reported_without_blocking_other_platforms(tmp_path):
    output = tmp_path / "raw.jsonl"

    def runner(platform, _keywords, destination, _timeout):
        if platform == "douyin":
            raise TimeoutError("login expired")
        destination.write_text(json.dumps(_item(platform, "ok", "算力继续看多"), ensure_ascii=False) + "\n")

    report = ss.collect_external(
        platforms=["douyin", "bili"], keywords=["算力"], raw_dir=tmp_path,
        runner=runner, trade_date="2026-08-10",
    )

    assert report["platforms"]["douyin"]["status"] == "failed"
    assert report["platforms"]["bili"]["status"] == "ok"
    assert report["coverage"] == pytest.approx(0.5)


def test_platforms_are_equal_weighted_when_building_daily_score():
    evidence = [
        {"platform": "bili", "author_hash": f"b{i}", "stance": 1.0, "confidence": 1.0,
         "fomo": 1.0, "panic": 0.0, "mapping_confidence": 0.9, "engagement": 100}
        for i in range(20)
    ] + [
        {"platform": "eastmoney", "author_hash": f"e{i}", "stance": -1.0, "confidence": 1.0,
         "fomo": 0.0, "panic": 1.0, "mapping_confidence": 0.9, "engagement": 1}
        for i in range(10)
    ]

    score = ss.build_daily_score(
        trade_date="2026-08-10", sector_id="concept:robot", sector_name="机器人",
        taxonomy="concept", evidence=evidence, history=[],
        market={"return": -1.0, "volume_change": -0.2, "breadth": 0.3, "fund_flow": -1.0},
    )

    assert score["platform_contributions"]["bili"]["net_sentiment"] == pytest.approx(1.0)
    assert score["platform_contributions"]["eastmoney"]["net_sentiment"] == pytest.approx(-1.0)
    assert score["net_sentiment"] == pytest.approx(0.0)
    assert score["market_divergence"] > 0


def test_briefing_only_reports_changes_and_quality_failures(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.create_alert("concept:robot", "机器人", "concept", "2026-08-10", "observe")

    lines = ss.briefing_changes(store, "2026-08-10", coverage=0.5)

    assert any("机器人" in line and "观察" in line for line in lines)
    assert any("覆盖不足" in line for line in lines)
    assert all("持续" not in line for line in lines)


def test_configured_pipeline_is_disabled_by_default():
    assert ss.run_configured({}) == {"status": "disabled"}


def test_configured_pipeline_maps_classifies_scores_and_opens_alert(tmp_path):
    def runner(platform, _keywords, destination, _timeout):
        rows = [
            _item(platform, f"{platform}-{i}", "机器人必须起飞，赶紧上车，坚定看多",
                  author_hash=f"{platform}-author-{i}", engagement=100)
            for i in range(12)
        ]
        destination.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))

    result = ss.run_configured({"sector_sentiment": {
        "enabled": True, "cache_dir": str(tmp_path), "platforms": ["bili", "eastmoney"],
        "keywords": ["机器人"], "taxonomy": [
            {"sector_id": "concept:robot", "sector_name": "机器人", "taxonomy": "concept",
             "aliases": ["机器人"]},
        ],
    }}, runner=runner, trade_date="2026-08-10")

    store = ss.open_store(tmp_path)
    assert result["status"] == "ok"
    assert store.scores_for_date("2026-08-10")[0]["sector_id"] == "concept:robot"
    assert store.get_state("concept:robot")["state"] == "observe"


def test_raw_scoring_features_survive_round_trip(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.save_daily_score({
        "trade_date": "2026-08-10", "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "platforms": ["bili", "eastmoney"], "independent_authors": 20,
        "mapping_confidence": 0.9, "sentiment_extreme": 0.8, "attention_acceleration": 0.7,
        "consensus_crowding": 0.6, "market_divergence": 0.5, "short_risk": 60,
        "swing_risk": 50, "attention_raw": 123.0, "sentiment_raw": 0.75,
    })
    assert store.scores_for_sector("concept:robot")[0]["attention_raw"] == 123.0
    assert store.scores_for_sector("concept:robot")[0]["sentiment_raw"] == 0.75


def test_mediacrawler_runner_exports_new_jsonl_to_canonical_destination(tmp_path):
    root = tmp_path / "MediaCrawler"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "data").mkdir()
    os.symlink(sys.executable, root / ".venv" / "bin" / "python")
    (root / "main.py").write_text(
        "from pathlib import Path\n"
        "Path('data/bili.jsonl').write_text('{\"video_id\":\"BV1\",\"title\":\"机器人起飞\"}\\n')\n"
    )
    destination = tmp_path / "canonical.jsonl"

    ss.default_mediacrawler_runner(root)("bili", ["机器人"], destination, 10)

    row = json.loads(destination.read_text())
    assert row["platform"] == "bili"
    assert row["content_id"] == "BV1"
    assert row["text"] == "机器人起飞"


def test_validation_go_requires_30_events_uplift_and_positive_cluster_ci():
    rows = [
        {"signal_date": f"2026-07-{i + 1:02d}", "short_hit": 1,
         "price_baseline_hit": int(i % 2 == 0), "heat_baseline_hit": 0}
        for i in range(30)
    ]
    result = ss.evaluate_layer(rows, "short_hit")
    assert result["n"] == 30
    assert result["uplift"] == pytest.approx(0.5)
    assert result["ci_low"] > 0
    assert result["verdict"] == "GO"


def test_financial_search_jobs_never_emit_bare_sector_terms():
    jobs = retrieval.build_search_jobs(
        [{"sector_id": "concept:robot", "sector_name": "机器人", "taxonomy": "concept",
          "aliases": ["机器人", "具身智能"]}],
        ["bili", "dy"],
        {"query_templates": ["{term} 股票", "{term} ETF"], "max_queries_per_sector": 3},
    )

    assert len(jobs) == 6
    assert {job.value for job in jobs} <= {"机器人 股票", "机器人 ETF", "具身智能 股票"}
    assert all(job.value not in {"机器人", "具身智能"} for job in jobs)
    assert all(job.mode == "search" for job in jobs)


def test_query_planning_is_stable_and_deduplicated():
    taxonomy = [{"sector_id": "concept:robot", "sector_name": "机器人",
                 "taxonomy": "concept", "aliases": ["机器人", "机器人"]}]
    first = retrieval.build_search_jobs(taxonomy, ["bili"], {
        "query_templates": ["{term} 股票", "{term} 股票"], "max_queries_per_sector": 8})
    second = retrieval.build_search_jobs(taxonomy, ["bili"], {
        "query_templates": ["{term} 股票", "{term} 股票"], "max_queries_per_sector": 8})

    assert first == second
    assert [job.value for job in first] == ["机器人 股票"]


def test_query_planning_rejects_unanchored_templates():
    taxonomy = [{"sector_id": "concept:robot", "sector_name": "机器人",
                 "taxonomy": "concept", "aliases": []}]

    with pytest.raises(ValueError, match="financial anchor"):
        retrieval.build_search_jobs(taxonomy, ["bili"], {
            "query_templates": ["{term}"], "max_queries_per_sector": 8})
