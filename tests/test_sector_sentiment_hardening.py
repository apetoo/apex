"""Regression contracts for the final financial-retrieval hardening wave."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from apex import sector_retrieval as retrieval
from apex import sector_sentiment as ss


PINNED_COMMIT = "a" * 40


def _item(content_id: str, text: str, **extra) -> dict:
    return {
        "platform": extra.pop("platform", "bili"),
        "content_id": content_id,
        "comment_id": extra.pop("comment_id", ""),
        "published_at": extra.pop("published_at", "2026-08-10T12:00:00+08:00"),
        "collected_at": extra.pop("collected_at", "2026-08-10T18:30:00+08:00"),
        "text": text,
        "title": extra.pop("title", text),
        "engagement": extra.pop("engagement", 10),
        **extra,
    }


def _retrieval_settings(**overrides) -> dict:
    settings = {
        "query_version": "sector-finance-query-v1",
        "relevance_version": "sector-finance-relevance-v1",
        "creator_rule_version": "sector-finance-creator-v1",
        "query_templates": ["{term} 股票"],
        "max_queries_per_sector": 1,
        "max_contents_per_query": 20,
        "max_comments_per_content": 50,
        "candidate_min_contents": 3,
    }
    settings.update(overrides)
    return settings


def _config(tmp_path: Path, **retrieval_overrides) -> dict:
    return {"sector_sentiment": {
        "enabled": True,
        "cache_dir": str(tmp_path),
        "mediacrawler_commit": PINNED_COMMIT,
        "semantic_prompt_version": "sector-semantic-v1",
        "platforms": ["bili"],
        "taxonomy": [{
            "sector_id": "concept:robot",
            "sector_name": "机器人",
            "taxonomy": "concept",
            "aliases": ["机器人"],
        }],
        "retrieval": _retrieval_settings(**retrieval_overrides),
    }}


def test_query_templates_require_term_and_a_separate_financial_anchor():
    taxonomy = [{
        "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "aliases": [],
    }]

    with pytest.raises(ValueError, match=r"\{term\}"):
        retrieval.build_search_jobs(taxonomy, ["bili"], {
            "query_templates": ["股票"], "max_queries_per_sector": 1,
        })
    with pytest.raises(ValueError, match="financial anchor"):
        retrieval.build_search_jobs(taxonomy, ["bili"], {
            "query_templates": ["{term}"], "max_queries_per_sector": 1,
        })
    with pytest.raises(ValueError, match="non-negative"):
        retrieval.build_search_jobs(taxonomy, ["bili"], {
            "query_templates": ["{term} 股票"], "max_queries_per_sector": -1,
        })


def test_blank_taxonomy_terms_never_render_a_bare_financial_query():
    jobs = retrieval.build_search_jobs([{
        "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "aliases": ["", "   "],
    }], ["bili"], {
        "query_templates": ["{term} 股票"], "max_queries_per_sector": 8,
    })
    assert [job.value for job in jobs] == ["机器人 股票"]

    with pytest.raises(ValueError, match="non-empty taxonomy term"):
        retrieval.build_search_jobs([{
            "sector_id": "concept:blank", "sector_name": " ",
            "taxonomy": "concept", "aliases": [""],
        }], ["bili"], {
            "query_templates": ["{term} 股票"], "max_queries_per_sector": 8,
        })


def test_equal_platform_queries_aggregate_every_matching_sector_id():
    taxonomy = [
        {"sector_id": "concept:robot", "sector_name": "机器人", "taxonomy": "concept", "aliases": []},
        {"sector_id": "industry:robot", "sector_name": "机器人", "taxonomy": "industry", "aliases": []},
    ]

    jobs = retrieval.build_search_jobs(
        taxonomy, ["bili"], {"query_templates": ["{term} 股票"], "max_queries_per_sector": 1},
    )

    assert len(jobs) == 1
    assert jobs[0].sector_ids == ("concept:robot", "industry:robot")


def test_anonymous_upstream_creator_hash_groups_candidates_and_uses_content_locator(tmp_path):
    normalized = ss.normalize_external_record({
        "video_id": "123456", "title": "机器人板块资金流入",
        "create_time": 1786320000, "creator_hash": "anonymous-creator-hash",
        "nickname": "财***王",
    }, "bili", trade_date="2026-08-10")
    assert normalized is not None
    assert normalized["author_id"] == "anonymous-creator-hash"

    normalized.update(
        relevance_score=0.9, relevance_decision="accepted",
        relevance_reasons=["path:entity_and_finance"],
        sector_ids=["concept:robot"],
    )
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.discover_creator_candidates(
        [normalized], _retrieval_settings(candidate_min_contents=1),
    )
    store.moderate_creator("bili", "anonymous-creator-hash", "approve")

    creator = store.approved_creators()[0]
    jobs = retrieval.build_creator_jobs([creator], ["bili"])

    assert creator["crawl_locator"] == "123456"
    assert jobs[0].value == "anonymous-creator-hash"
    assert jobs[0].crawl_locator == "123456"


def test_financial_relevance_has_three_explicit_admission_paths_and_audit_reasons():
    entity_and_finance = retrieval.classify_financial_relevance(
        {"title": "机器人板块资金流入", "text": "继续看多"}, ["机器人"], False,
    )
    approved_investment_context = retrieval.classify_financial_relevance(
        {"title": "今日复盘", "text": "继续加仓，当前持仓上升"}, ["机器人"], True,
    )
    low_confidence = retrieval.classify_financial_relevance(
        {"title": "机器人产业近况", "text": "行业出现变化"}, ["机器人"], False,
    )
    no_hit = retrieval.classify_financial_relevance(
        {"title": "周末随拍", "text": "天气很好"}, ["机器人"], False,
    )

    assert entity_and_finance.decision == "accepted"
    assert "path:entity_and_finance" in entity_and_finance.reasons
    assert approved_investment_context.decision == "accepted"
    assert "path:approved_author_investment" in approved_investment_context.reasons
    assert low_confidence.decision == "review"
    assert "audit:low_confidence_requires_llm" in low_confidence.reasons
    assert no_hit.decision == "filtered_non_financial"
    assert "audit:no_sector_or_finance_match" in no_hit.reasons


def test_entity_admission_requires_headline_metadata_and_semantic_text_survives_ingest(tmp_path):
    body_only = retrieval.classify_financial_relevance(
        {"title": "今日观察", "text": "机器人股票资金持续流入"}, ["机器人"], False,
    )
    metadata_entity = retrieval.classify_financial_relevance(
        {"title": "机器人板块观察", "text": "资金持续流入"}, ["机器人"], False,
    )

    assert body_only.decision == "review"
    assert metadata_entity.decision == "accepted"

    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    item = _item("structured", "资金持续流入", title="机器人板块观察")
    item["trade_date"] = "2026-08-10"
    store.ingest([item], [metadata_entity])
    stored = store.list_eligible_content("2026-08-10")[0]

    assert "机器人板块观察" in stored["semantic_text"]
    assert ss._semantic_evidence(stored, [{
        "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "aliases": ["机器人"],
    }])[0]["sector_id"] == "concept:robot"


def test_relevance_llm_accepts_only_the_fixed_exact_schema(monkeypatch):
    from apex import llm

    monkeypatch.setattr(llm, "chat", lambda *_args, **_kwargs: {"content": json.dumps({
        "financial_relevant": True,
        "confidence": 0.82,
        "reasons": ["投资语境明确"],
    }, ensure_ascii=False)})

    decision = ss.llm_financial_relevance(
        _item("review", "机器人产业近况"), ["机器人"], "sector-finance-relevance-v1",
    )

    assert decision.decision == "accepted"
    assert decision.score == pytest.approx(0.82)
    assert decision.version == "sector-finance-relevance-v1"
    assert "llm:投资语境明确" in decision.reasons


def test_relevance_and_semantic_llms_receive_the_frozen_actual_models(monkeypatch):
    from apex import llm

    calls = []
    responses = iter([
        {"financial_relevant": True, "confidence": 0.8, "reasons": ["金融语境"]},
        [{
            "sector_id": "concept:robot", "stance": 0.5, "confidence": 0.8,
            "fomo": 0.2, "panic": 0.1, "narrative": "景气改善",
            "evidence_span": "机器人板块资金流入",
        }],
    ])

    def chat(*_args, **kwargs):
        calls.append(kwargs)
        return {"content": json.dumps(next(responses), ensure_ascii=False)}

    monkeypatch.setattr(llm, "chat", chat)
    ss.llm_financial_relevance(
        _item("review", "机器人产业近况"), ["机器人"],
        "sector-finance-relevance-v1", model="deepseek-relevance-pinned",
    )
    ss.llm_semantic_evidence(
        _item("semantic", "机器人板块资金流入"), [{
            "sector_id": "concept:robot", "sector_name": "机器人",
            "taxonomy": "concept", "aliases": ["机器人"],
        }], model="deepseek-semantic-pinned",
    )

    assert [call["model"] for call in calls] == [
        "deepseek-relevance-pinned", "deepseek-semantic-pinned",
    ]


def test_relevance_llm_never_receives_contact_details(monkeypatch):
    from apex import llm

    captured = {}

    def chat(messages, **_kwargs):
        captured["messages"] = messages
        return {"content": json.dumps({
            "financial_relevant": False, "confidence": 0.9,
            "reasons": ["金融语境不足"],
        }, ensure_ascii=False)}

    monkeypatch.setattr(llm, "chat", chat)
    ss.llm_financial_relevance(_item(
        "private", "电话 +86 138-0013-8000，微信：财经小王",
        title="邮箱 trader@example.com", description="微博：https://weibo.com/private",
    ), ["机器人"], "sector-finance-relevance-v1")

    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    for secret in (
        "138-0013-8000", "财经小王", "trader@example.com", "weibo.com/private",
    ):
        assert secret not in prompt
    assert "已脱敏" in prompt


@pytest.mark.parametrize("payload", [
    {"financial_relevant": True, "confidence": 0.8, "reasons": [], "stance": 1},
    {"financial_relevant": True, "confidence": 1.1, "reasons": []},
    {"financial_relevant": "true", "confidence": 0.8, "reasons": []},
    {"financial_relevant": True, "confidence": float("nan"), "reasons": []},
])
def test_relevance_llm_rejects_unknown_wrong_type_and_non_finite_fields(monkeypatch, payload):
    from apex import llm

    monkeypatch.setattr(
        llm, "chat", lambda *_args, **_kwargs: {"content": json.dumps(payload)},
    )

    with pytest.raises(ValueError, match="relevance LLM"):
        ss.llm_financial_relevance(
            _item("review", "机器人产业近况"), ["机器人"], "sector-finance-relevance-v1",
        )


def test_relevance_llm_failure_isolated_per_record_with_safe_filtered_fallback(tmp_path):
    path = tmp_path / "review.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [
        _item("bad", "机器人产业近况"),
        _item("good", "机器人行业近况"),
    ]))
    report = {"jobs": [{
        "status": "ok", "path": str(path), "mode": "search",
        "source_id": "query:机器人 股票", "sector_ids": ["concept:robot"],
        "trade_date": "2026-08-10", "cutoff": None,
    }]}

    def classifier(record, _terms, _version):
        if record["content_id"] == "bad":
            raise ValueError("malformed response")
        return retrieval.RelevanceDecision(
            0.8, "accepted", ("llm:投资语境明确",), "sector-finance-relevance-v1",
        )

    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    result = ss._ingest_and_classify_relevance(
        report, store, [{
            "sector_id": "concept:robot", "sector_name": "机器人",
            "taxonomy": "concept", "aliases": ["机器人"],
        }], relevance_llm_classifier=classifier,
        relevance_llm_enabled=True, relevance_version="sector-finance-relevance-v1",
    )

    rows = {row["content_id"]: row for row in store.list_content("2026-08-10")}
    assert report["jobs"][0]["status"] == "ok"
    assert result["quarantined"] == 0
    assert rows["bad"]["relevance_decision"] == "filtered_non_financial"
    assert "relevance_llm_error" in rows["bad"]["relevance_reasons_json"]
    assert rows["good"]["relevance_decision"] == "accepted"


def test_semantic_llm_accepts_only_exact_whitelisted_finite_schema(monkeypatch):
    from apex import llm

    valid = [{
        "sector_id": "concept:robot", "stance": 0.5, "confidence": 0.8,
        "fomo": 0.2, "panic": 0.1, "narrative": "景气改善",
        "evidence_span": "机器人板块观察",
    }]
    monkeypatch.setattr(llm, "chat", lambda *_args, **_kwargs: {"content": json.dumps(valid, ensure_ascii=False)})

    evidence = ss.llm_semantic_evidence(_item(
        "semantic", "资金持续流入", title="机器人板块观察",
    ), [{
        "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "aliases": ["机器人"],
    }])

    assert evidence[0]["sector_id"] == "concept:robot"
    assert evidence[0]["stance"] == 0.5
    assert evidence[0]["text"] == "机器人板块观察"


@pytest.mark.parametrize("mutation", [
    {"extra": "forbidden"},
    {"sector_id": "concept:unknown"},
    {"stance": 1.01},
    {"confidence": -0.01},
    {"fomo": float("inf")},
    {"panic": "0.1"},
])
def test_semantic_llm_rejects_unknown_sector_fields_and_invalid_ranges(monkeypatch, mutation):
    from apex import llm

    value = {
        "sector_id": "concept:robot", "stance": 0.5, "confidence": 0.8,
        "fomo": 0.2, "panic": 0.1, "narrative": "景气改善",
        "evidence_span": "机器人板块资金流入",
        **mutation,
    }
    monkeypatch.setattr(llm, "chat", lambda *_args, **_kwargs: {"content": json.dumps([value])})

    with pytest.raises(ValueError, match="semantic LLM"):
        ss.llm_semantic_evidence(_item("semantic", "机器人板块资金流入"), [{
            "sector_id": "concept:robot", "sector_name": "机器人",
            "taxonomy": "concept", "aliases": ["机器人"],
        }])


def test_semantic_llm_rejects_duplicate_sectors_and_unbound_evidence_span(monkeypatch):
    from apex import llm

    value = {
        "sector_id": "concept:robot", "stance": 0.5, "confidence": 0.8,
        "fomo": 0.2, "panic": 0.1, "narrative": "景气改善",
        "evidence_span": "原文里不存在的证据",
    }
    monkeypatch.setattr(llm, "chat", lambda *_args, **_kwargs: {
        "content": json.dumps([value]),
    })
    with pytest.raises(ValueError, match="evidence_span"):
        ss.llm_semantic_evidence(_item("semantic", "机器人板块资金流入"), [{
            "sector_id": "concept:robot", "sector_name": "机器人",
            "taxonomy": "concept", "aliases": ["机器人"],
        }])

    value["evidence_span"] = "机器人板块资金流入"
    monkeypatch.setattr(llm, "chat", lambda *_args, **_kwargs: {
        "content": json.dumps([value, value]),
    })
    with pytest.raises(ValueError, match="duplicate"):
        ss.llm_semantic_evidence(_item("semantic", "机器人板块资金流入"), [{
            "sector_id": "concept:robot", "sector_name": "机器人",
            "taxonomy": "concept", "aliases": ["机器人"],
        }])


def test_explicit_trade_date_is_persisted_independently_from_utc_collection_time(tmp_path):
    def runner(job, destination, _timeout, _limits):
        assert job.trade_date == "2026-08-10"
        destination.write_text(json.dumps(_item(
            "utc-next-day", "机器人板块资金流入",
            collected_at="2026-08-11T00:30:00+00:00",
        ), ensure_ascii=False) + "\n")

    ss.run_configured(_config(tmp_path), runner=runner, trade_date="2026-08-10")
    store = ss.open_store(tmp_path)

    assert [row["content_id"] for row in store.list_eligible_content("2026-08-10")] == [
        "utc-next-day",
    ]
    assert store.list_eligible_content("2026-08-11") == []
    assert store.list_content("2026-08-10")[0]["collected_at"] == "2026-08-11T00:30:00+00:00"


def test_creator_approval_timestamp_is_immutable_and_carried_as_job_cutoff(tmp_path, monkeypatch):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    store.discover_creator_candidates([{
        **_item("v1", "机器人板块资金流入"),
        "author_id": "up-1", "author_name": "财经小王",
        "relevance_score": 0.9, "relevance_decision": "accepted",
        "sector_ids": ["concept:robot"],
    }], _retrieval_settings(candidate_min_contents=1, config_hash="policy-hash"))
    monkeypatch.setattr(ss, "_now", lambda: "2026-08-10T02:00:00+00:00")
    first = store.moderate_creator("bili", "up-1", "approve")
    monkeypatch.setattr(ss, "_now", lambda: "2026-08-11T02:00:00+00:00")
    second = store.moderate_creator("bili", "up-1", "approve")

    jobs = retrieval.build_creator_jobs(
        [second], ["bili"], trade_date="2026-08-11", config_hash="policy-hash",
    )

    assert first["approved_at"] == "2026-08-10T02:00:00+00:00"
    assert second["approved_at"] == first["approved_at"]
    assert jobs[0].cutoff == first["approved_at"]
    assert jobs[0].trade_date == "2026-08-11"


def test_creator_published_at_cutoff_is_rechecked_at_ingress_and_invalid_dates_quarantined(tmp_path):
    path = tmp_path / "creator.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [
        _item("old", "继续加仓", published_at="2026-08-10T01:59:59+00:00"),
        _item("new", "继续加仓", published_at="2026-08-10T02:00:01+00:00"),
        _item("missing", "继续加仓", published_at=None),
        _item("invalid", "继续加仓", published_at="not-a-date"),
    ]))
    report = {"jobs": [{
        "status": "ok", "path": str(path), "mode": "creator",
        "source_id": "creator:bili:up-1", "sector_ids": ["concept:robot"],
        "trade_date": "2026-08-10", "cutoff": "2026-08-10T02:00:00+00:00",
    }]}
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")

    result = ss._ingest_and_classify_relevance(report, store, [{
        "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "aliases": ["机器人"],
    }])

    assert [row["content_id"] for row in store.list_eligible_content("2026-08-10")] == ["new"]
    assert result["quarantined"] == 3
    assert result["quarantine_reasons"] == {
        "creator_before_approval": 1,
        "creator_missing_published_at": 1,
        "creator_invalid_published_at": 1,
    }


def test_creator_comment_cannot_reintroduce_a_preapproval_parent_content(tmp_path):
    path = tmp_path / "creator-comments.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [
        _item("old", "旧内容", published_at="2026-08-10T01:00:00+00:00"),
        _item(
            "old", "批准后新增评论：继续加仓", comment_id="comment-after-approval",
            published_at="2026-08-10T03:00:00+00:00",
        ),
        _item("new", "新内容：继续加仓", published_at="2026-08-10T03:00:00+00:00"),
        _item(
            "new", "新内容评论：继续加仓", comment_id="new-comment",
            published_at="2026-08-10T03:01:00+00:00",
        ),
        _item(
            "orphan", "没有父内容的评论", comment_id="orphan-comment",
            published_at="2026-08-10T03:01:00+00:00",
        ),
    ]))
    report = {"jobs": [{
        "status": "ok", "path": str(path), "mode": "creator",
        "source_id": "creator:bili:up-1", "sector_ids": ["concept:robot"],
        "trade_date": "2026-08-10", "cutoff": "2026-08-10T02:00:00+00:00",
    }]}
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")

    result = ss._ingest_and_classify_relevance(report, store, [{
        "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "aliases": ["机器人"],
    }])

    assert {(row["content_id"], row["comment_id"])
            for row in store.list_content("2026-08-10")} == {
        ("new", ""), ("new", "new-comment"),
    }
    assert result["quarantine_reasons"] == {
        "creator_before_approval": 1,
        "creator_parent_before_approval": 1,
        "creator_missing_parent_content": 1,
    }


def test_runner_passes_real_cli_limits_and_uses_an_isolated_output_boundary(tmp_path, monkeypatch):
    root = tmp_path / "MediaCrawler"
    root.mkdir()
    (root / "main.py").write_text("# boundary")
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{PINNED_COMMIT}\n")
        if command[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(stdout="")
        stage = Path(command[command.index("--save_data_path") + 1])
        output = stage / "bili" / "jsonl" / "search_contents_2026-08-10.jsonl"
        output.parent.mkdir(parents=True)
        output.write_text(json.dumps({
            "content_id": "c1", "text": "机器人板块资金流入",
            "published_at": "2026-08-10T01:00:00+00:00",
        }, ensure_ascii=False) + "\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    destination = tmp_path / "canonical.jsonl"
    ss.default_mediacrawler_runner(root, commit=PINNED_COMMIT)(
        retrieval.RetrievalJob(
            "bili", "search", "机器人 股票", ("concept:robot",), "query:机器人 股票",
            trade_date="2026-08-10",
        ), destination, 10, {"max_contents": 7, "max_comments": 3},
    )

    command = commands[-1]
    assert command[1].endswith("mediacrawler_bounded.py") is False
    assert command[command.index("--crawler_max_notes_count") + 1] == "7"
    assert command[command.index("--max_comments_count_singlenotes") + 1] == "3"
    assert command[command.index("--max_concurrency_num") + 1] == "1"
    assert command[command.index("--get_sub_comment") + 1] == "false"
    assert command[command.index("--save_data_option") + 1] == "jsonl"
    assert Path(command[command.index("--save_data_path") + 1]).is_relative_to(destination.parent)
    assert json.loads(destination.read_text())["content_id"] == "c1"


def test_runner_quarantines_bad_upstream_rows_without_losing_good_rows(tmp_path, monkeypatch):
    root = tmp_path / "MediaCrawler"
    root.mkdir()
    (root / "main.py").write_text("# boundary")

    def fake_run(command, **_kwargs):
        stage = Path(command[command.index("--save_data_path") + 1])
        output = stage / "bili" / "jsonl" / "search_contents.jsonl"
        output.parent.mkdir(parents=True)
        output.write_text("\n".join([
            "{bad-json",
            json.dumps({"content_id": "bad-none", "text": "坏", "liked_count": "None"}),
            json.dumps({"content_id": "bad-inf", "text": "坏", "engagement": float("inf")}),
            json.dumps({"content_id": "good", "text": "机器人板块资金流入", "liked_count": "7"}),
        ]) + "\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    destination = tmp_path / "canonical.jsonl"
    ss.default_mediacrawler_runner(root)(
        retrieval.RetrievalJob(
            "bili", "search", "机器人 股票", ("concept:robot",), "query:机器人 股票",
            trade_date="2026-08-10",
        ), destination, 10, {"max_contents": 7, "max_comments": 3},
    )

    rows = [json.loads(line) for line in destination.read_text().splitlines()]
    assert [(row["content_id"], row["engagement"]) for row in rows] == [("good", 7.0)]


def test_ingest_does_not_misreport_unrelated_integrity_errors_as_duplicates(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """CREATE TRIGGER reject_test_content BEFORE INSERT ON content
               WHEN NEW.content_id='explode'
               BEGIN SELECT RAISE(ABORT, 'test constraint'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="test constraint"):
        store.ingest([_item("explode", "机器人板块资金流入")])


def test_creator_runner_uses_process_local_bounded_adapter_and_rejects_dirty_pin(
    tmp_path, monkeypatch,
):
    root = tmp_path / "MediaCrawler"
    root.mkdir()
    (root / "main.py").write_text("# boundary")
    commands = []
    creator_process = {}

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{PINNED_COMMIT}\n")
        if command[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(stdout="")
        creator_process.update(kwargs)
        stage = Path(command[command.index("--save_data_path") + 1])
        output = stage / "bili" / "jsonl" / "creator_contents_2026-08-10.jsonl"
        output.parent.mkdir(parents=True)
        output.write_text(json.dumps({
            "content_id": "c1", "text": "继续加仓",
            "published_at": "2026-08-10T03:00:00+00:00",
        }, ensure_ascii=False) + "\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    destination = tmp_path / "creator-canonical.jsonl"
    job = retrieval.RetrievalJob(
        "bili", "creator", "up-1", ("concept:robot",), "creator:bili:up-1",
        trade_date="2026-08-10", cutoff="2026-08-10T02:00:00+00:00",
        crawl_locator="123456",
    )
    ss.default_mediacrawler_runner(root, commit=PINNED_COMMIT)(
        job, destination, 10, {"max_contents": 7, "max_comments": 3},
    )

    command = commands[-1]
    assert command[1].endswith("mediacrawler_bounded.py")
    assert command[command.index("--creator_id") + 1] == "123456"
    assert creator_process["env"]["APEX_CREATOR_CONTENT_ID"] == "123456"
    assert creator_process["env"]["APEX_APPROVED_CREATOR_HASH"] == "up-1"
    assert json.loads(destination.read_text())["content_id"] == "c1"

    def dirty_run(command, **_kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{PINNED_COMMIT}\n")
        if command[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(
                stdout=" M media_platform/douyin/core.py\n?? config.py\n",
            )
        raise AssertionError("dirty checkout must fail before collection")

    monkeypatch.setattr(ss.subprocess, "run", dirty_run)
    with pytest.raises(RuntimeError, match="working tree is dirty"):
        ss.default_mediacrawler_runner(root, commit=PINNED_COMMIT)(
            job, tmp_path / "dirty.jsonl", 10,
            {"max_contents": 7, "max_comments": 3},
        )


def test_process_local_creator_adapter_caps_bili_and_douyin_before_detail_fetch():
    from apex.mediacrawler_bounded import install_creator_bounds

    class BiliClient:
        def __init__(self):
            self.calls = 0

        async def get_creator_videos(self, _creator_id, _page, _page_size):
            self.calls += 1
            return {
                "list": {"vlist": [{"bvid": f"BV{index}"} for index in range(30)]},
                "page": {"count": 300},
            }

    class BiliCrawler:
        def __init__(self):
            self.bili_client = BiliClient()
            self.fetched = []

        async def get_specified_videos(self, video_ids):
            self.fetched.extend(video_ids)

    class DouyinClient:
        def __init__(self):
            self.calls = 0

        async def get_user_info(self, _creator_id):
            return {}

        async def get_user_aweme_posts(self, _creator_id, _cursor):
            self.calls += 1
            return {
                "has_more": 1, "max_cursor": str(self.calls),
                "aweme_list": [{"aweme_id": f"dy-{self.calls}-{index}"} for index in range(18)],
            }

    config = SimpleNamespace(CRAWLER_MAX_NOTES_COUNT=3, CRAWLER_MAX_SLEEP_SEC=0)
    install_creator_bounds(
        SimpleNamespace(BilibiliCrawler=BiliCrawler), config, DouyinClient,
    )

    bili = BiliCrawler()
    asyncio.run(bili.get_creator_videos(1))
    assert bili.bili_client.calls == 1
    assert bili.fetched == ["BV0", "BV1", "BV2"]

    callback_rows = []

    async def callback(rows):
        callback_rows.extend(rows)

    douyin = DouyinClient()
    values = asyncio.run(douyin.get_all_user_aweme_posts("sec-user", callback))
    assert douyin.calls == 1
    assert len(values) == 3
    assert callback_rows == values


def test_creator_adapter_verifies_locator_against_the_approved_anonymous_hash():
    from apex.mediacrawler_bounded import install_creator_bounds

    class BiliClient:
        def __init__(self):
            self.creator_ids = []

        async def get_video_info(self, **_kwargs):
            return {"View": {"owner": {"mid": 42}}}

        async def get_creator_videos(self, creator_id, _page, _page_size):
            self.creator_ids.append(creator_id)
            return {"list": {"vlist": []}, "page": {"count": 0}}

    class BiliCrawler:
        def __init__(self):
            self.bili_client = BiliClient()

        async def get_specified_videos(self, _video_ids):
            raise AssertionError("empty creator page must not fetch details")

    class DouyinClient:
        async def get_user_info(self, _creator_id):
            return {}

    install_creator_bounds(
        SimpleNamespace(BilibiliCrawler=BiliCrawler),
        SimpleNamespace(CRAWLER_MAX_NOTES_COUNT=3), DouyinClient,
        creator_locator="123456", approved_creator_hash="hash:42",
        anonymize_user_id=lambda value: f"hash:{value}",
    )
    crawler = BiliCrawler()
    asyncio.run(crawler.get_creator_videos(123456))
    assert crawler.bili_client.creator_ids == [42]

    class WrongBiliCrawler(BiliCrawler):
        pass

    install_creator_bounds(
        SimpleNamespace(BilibiliCrawler=WrongBiliCrawler),
        SimpleNamespace(CRAWLER_MAX_NOTES_COUNT=3), DouyinClient,
        creator_locator="123456", approved_creator_hash="hash:99",
        anonymize_user_id=lambda value: f"hash:{value}",
    )
    with pytest.raises(RuntimeError, match="does not match"):
        asyncio.run(WrongBiliCrawler().get_creator_videos(123456))


def test_persisted_daily_job_budget_prevents_duplicate_collection(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    calls = []
    job = retrieval.RetrievalJob(
        "bili", "search", "机器人 股票", ("concept:robot",), "query:机器人 股票",
        trade_date="2026-08-10", config_hash="policy-hash",
    )

    def runner(_job, destination, _timeout, _limits):
        calls.append(_job)
        destination.write_text(json.dumps(_item("once", "机器人板块资金流入")) + "\n")

    first = ss.collect_jobs(
        [job], runner, tmp_path / "raw", "2026-08-10",
        {"max_contents": 20, "max_comments": 50}, store=store,
    )
    ss._ingest_and_classify_relevance(first, store, [{
        "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "aliases": ["机器人"],
    }])
    second = ss.collect_jobs(
        [job], runner, tmp_path / "raw", "2026-08-10",
        {"max_contents": 20, "max_comments": 50}, store=ss.SentimentStore(store.path),
    )

    assert len(calls) == 1
    assert first["jobs"][0]["status"] == "ok"
    assert second["jobs"][0]["status"] == "budget_reused"
    assert second["coverage"] == 1.0
    assert store.collection_budgets("2026-08-10") == [{
        "trade_date": "2026-08-10", "job_key": job.budget_key,
        "mode": "search", "platform": "bili", "max_contents": 20,
        "max_comments_per_content": 50, "status": "completed",
        "config_hash": "policy-hash",
    }]


def test_failed_daily_job_budget_is_not_retried_or_reported_as_covered(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    job = retrieval.RetrievalJob(
        "bili", "search", "机器人 股票", ("concept:robot",), "query:机器人 股票",
        trade_date="2026-08-10", config_hash="policy-hash",
    )
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        raise TimeoutError("collector timeout")

    first = ss.collect_jobs(
        [job], runner, tmp_path / "raw", "2026-08-10",
        {"max_contents": 20, "max_comments": 50}, store=store,
    )
    second = ss.collect_jobs(
        [job], runner, tmp_path / "raw", "2026-08-10",
        {"max_contents": 20, "max_comments": 50}, store=store,
    )

    assert calls == 1
    assert first["coverage"] == 0.0
    assert second["coverage"] == 0.0
    assert second["jobs"][0]["status"] == "budget_exhausted"


def test_ingest_failure_marks_consumed_budget_failed_instead_of_reused_coverage(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    job = retrieval.RetrievalJob(
        "bili", "creator", "opaque", ("concept:robot",), "creator:bili:opaque",
        trade_date="2026-08-10", cutoff="2026-08-10T02:00:00+00:00",
        config_hash="policy-hash",
    )

    def runner(_job, destination, _timeout, _limits):
        destination.write_text(json.dumps(_item(
            "bad-date", "继续加仓", published_at="not-a-date",
        )) + "\n")

    first = ss.collect_jobs(
        [job], runner, tmp_path / "raw", "2026-08-10",
        {"max_contents": 20, "max_comments": 50}, store=store,
    )
    ss._ingest_and_classify_relevance(first, store, [{
        "sector_id": "concept:robot", "sector_name": "机器人",
        "taxonomy": "concept", "aliases": ["机器人"],
    }])
    second = ss.collect_jobs(
        [job], runner, tmp_path / "raw", "2026-08-10",
        {"max_contents": 20, "max_comments": 50}, store=store,
    )

    assert first["coverage"] == 0.0
    assert second["coverage"] == 0.0
    assert second["jobs"][0]["status"] == "budget_exhausted"


def test_daily_job_budget_does_not_reuse_prior_data_under_a_different_policy(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    first_job = retrieval.RetrievalJob(
        "bili", "search", "机器人 股票", ("concept:robot",), "query:机器人 股票",
        trade_date="2026-08-10", config_hash="policy-v1",
    )
    changed_job = retrieval.RetrievalJob(
        "bili", "search", "机器人 股票", ("concept:robot",), "query:机器人 股票",
        trade_date="2026-08-10", config_hash="policy-v2",
    )

    def runner(_job, destination, _timeout, _limits):
        destination.write_text(json.dumps(_item("once", "机器人板块资金流入")) + "\n")

    ss.collect_jobs(
        [first_job], runner, tmp_path / "raw", "2026-08-10",
        {"max_contents": 20, "max_comments": 50}, store=store,
    )
    second = ss.collect_jobs(
        [changed_job], runner, tmp_path / "raw", "2026-08-10",
        {"max_contents": 20, "max_comments": 50}, store=store,
    )

    assert second["coverage"] == 0.0
    assert second["jobs"][0]["status"] == "budget_config_mismatch"


def test_enabled_pipeline_requires_pin_and_exact_frozen_versions(tmp_path):
    missing_pin = _config(tmp_path)
    missing_pin["sector_sentiment"]["mediacrawler_commit"] = ""
    with pytest.raises(ValueError, match="mediacrawler_commit"):
        ss.run_configured(missing_pin, runner=lambda *_args: None, trade_date="2026-08-10")

    mismatched = _config(tmp_path)
    mismatched["sector_sentiment"]["retrieval"]["query_version"] = "query-v2"
    with pytest.raises(ValueError, match="query_version"):
        ss.run_configured(mismatched, runner=lambda *_args: None, trade_date="2026-08-10")


def test_policy_hash_covers_pin_llm_toggle_taxonomy_and_is_frozen_for_sixty_days(tmp_path):
    cfg = _config(tmp_path)
    settings = cfg["sector_sentiment"]
    base = ss._validate_retrieval_policy(settings, settings["retrieval"])

    changed = _config(tmp_path)
    changed["sector_sentiment"]["mediacrawler_commit"] = "b" * 40
    assert ss._validate_retrieval_policy(
        changed["sector_sentiment"], changed["sector_sentiment"]["retrieval"],
    ) != base

    changed = _config(tmp_path, relevance_llm_enabled=True)
    changed["sector_sentiment"]["relevance_llm_model"] = "deepseek-chat"
    assert ss._validate_retrieval_policy(
        changed["sector_sentiment"], changed["sector_sentiment"]["retrieval"],
    ) != base

    changed = _config(tmp_path)
    changed["sector_sentiment"]["semantic_llm_model"] = "deepseek-reasoner"
    assert ss._validate_retrieval_policy(
        changed["sector_sentiment"], changed["sector_sentiment"]["retrieval"],
    ) != base

    changed = _config(tmp_path)
    changed["sector_sentiment"]["taxonomy"][0]["aliases"].append("人形机器人")
    assert ss._validate_retrieval_policy(
        changed["sector_sentiment"], changed["sector_sentiment"]["retrieval"],
    ) != base

    store = ss.SentimentStore(tmp_path / "frozen.sqlite3")
    store.record_collection({
        "trade_date": "2026-08-01", "coverage": 1.0,
        "search_coverage": 1.0, "creator_coverage": 1.0,
        "platforms": {}, "funnel": {}, "config_hash": base,
    })
    with pytest.raises(ValueError, match="frozen"):
        store.validate_frozen_policy("2026-08-10", "different-policy")


def test_policy_freeze_uses_sixty_distinct_trading_dates_and_allows_next_cohort(tmp_path):
    store = ss.SentimentStore(tmp_path / "cohorts.sqlite3")

    def record(day: str, policy: str) -> None:
        store.record_collection({
            "trade_date": day, "coverage": 1.0,
            "search_coverage": 1.0, "creator_coverage": 1.0,
            "platforms": {}, "funnel": {}, "config_hash": policy,
        })

    start = date(2026, 1, 1)
    for offset in range(59):
        record((start + timedelta(days=offset)).isoformat(), "policy-v1")

    with pytest.raises(ValueError, match="frozen"):
        store.validate_frozen_policy(
            (start + timedelta(days=59)).isoformat(), "policy-v2",
        )

    sixtieth = (start + timedelta(days=59)).isoformat()
    record(sixtieth, "policy-v1")
    next_day = (start + timedelta(days=60)).isoformat()
    store.validate_frozen_policy(next_day, "policy-v2")
    record(next_day, "policy-v2")

    store.validate_frozen_policy(
        (start + timedelta(days=20, hours=0)).isoformat(), "policy-v1",
    )
    with pytest.raises(ValueError, match="frozen"):
        store.validate_frozen_policy(
            (start + timedelta(days=20)).isoformat(), "policy-v2",
        )


def test_policy_override_keeps_default_freeze_without_a_reason(tmp_path):
    store = ss.SentimentStore(tmp_path / "policy-override.sqlite3")
    store.reserve_policy_day("2026-08-10", "policy-v1")

    with pytest.raises(ValueError, match="frozen"):
        store.reserve_policy_day("2026-08-11", "policy-v2")


def test_policy_override_starts_audited_future_cohort_without_rewriting_history(tmp_path):
    store = ss.SentimentStore(tmp_path / "policy-override.sqlite3")
    old_cohort = store.reserve_policy_day("2026-08-10", "policy-v1")
    with sqlite3.connect(store.path) as conn:
        historical_before = conn.execute(
            """SELECT trade_date, config_hash, policy_cohort_id, cohort_start_date,
                      status, reserved_at, completed_at
               FROM policy_day_reservations ORDER BY trade_date"""
        ).fetchall()

    new_cohort = store.reserve_policy_day(
        "2026-08-11", "policy-v2",
        override_reason="升级 MediaCrawler 并降低 B站/抖音采集配额",
    )

    assert new_cohort["policy_cohort_id"] != old_cohort["policy_cohort_id"]
    assert new_cohort["cohort_start_date"] == "2026-08-11"
    with sqlite3.connect(store.path) as conn:
        historical_after = conn.execute(
            """SELECT trade_date, config_hash, policy_cohort_id, cohort_start_date,
                      status, reserved_at, completed_at
               FROM policy_day_reservations WHERE trade_date<'2026-08-11'
               ORDER BY trade_date"""
        ).fetchall()
        migration = conn.execute(
            """SELECT effective_trade_date, old_config_hash, old_policy_cohort_id,
                      new_config_hash, new_policy_cohort_id, reason, created_at
               FROM policy_migrations"""
        ).fetchone()

    assert historical_after == historical_before
    assert migration[:6] == (
        "2026-08-11",
        "policy-v1",
        old_cohort["policy_cohort_id"],
        "policy-v2",
        new_cohort["policy_cohort_id"],
        "升级 MediaCrawler 并降低 B站/抖音采集配额",
    )
    assert migration[6]


def test_policy_override_cannot_replace_a_same_day_reservation(tmp_path):
    store = ss.SentimentStore(tmp_path / "policy-override.sqlite3")
    store.reserve_policy_day("2026-08-10", "policy-v1")

    with pytest.raises(ValueError, match="reserved|historical|conflict"):
        store.reserve_policy_day(
            "2026-08-10", "policy-v2", override_reason="紧急升级采集器",
        )

    with sqlite3.connect(store.path) as conn:
        reservation = conn.execute(
            "SELECT config_hash FROM policy_day_reservations WHERE trade_date='2026-08-10'"
        ).fetchone()
        migration_count = conn.execute("SELECT COUNT(*) FROM policy_migrations").fetchone()[0]
    assert reservation == ("policy-v1",)
    assert migration_count == 0


def test_policy_override_retry_is_idempotent_and_does_not_duplicate_audit(tmp_path):
    store = ss.SentimentStore(tmp_path / "policy-override.sqlite3")
    store.reserve_policy_day("2026-08-10", "policy-v1")
    first = store.reserve_policy_day(
        "2026-08-11", "policy-v2", override_reason="升级采集器",
    )

    second = store.reserve_policy_day(
        "2026-08-11", "policy-v2", override_reason="重跑时文案不同也不重复迁移",
    )

    assert second == first
    with sqlite3.connect(store.path) as conn:
        migration_count = conn.execute("SELECT COUNT(*) FROM policy_migrations").fetchone()[0]
        reservation_count = conn.execute(
            "SELECT COUNT(*) FROM policy_day_reservations WHERE trade_date='2026-08-11'"
        ).fetchone()[0]
    assert migration_count == 1
    assert reservation_count == 1


def test_policy_override_does_not_apply_new_cohort_before_its_effective_date(tmp_path):
    store = ss.SentimentStore(tmp_path / "policy-override.sqlite3")
    old_cohort = store.reserve_policy_day("2026-08-10", "policy-v1")
    store.reserve_policy_day(
        "2026-08-12", "policy-v2", override_reason="升级采集器",
    )

    gap_day = store.reserve_policy_day("2026-08-11", "policy-v1")

    assert gap_day == old_cohort
    assert gap_day["cohort_start_date"] <= "2026-08-11"
    with pytest.raises(ValueError, match="historical|conflict|frozen"):
        store.reserve_policy_day("2026-08-11", "policy-v2")


def test_policy_override_rejects_reason_when_policy_hash_is_unchanged(tmp_path):
    store = ss.SentimentStore(tmp_path / "policy-override.sqlite3")
    store.reserve_policy_day("2026-08-10", "policy-v1")

    with pytest.raises(ValueError, match="override|unchanged|same|identical"):
        store.reserve_policy_day(
            "2026-08-11", "policy-v1", override_reason="升级采集器",
        )

    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM policy_migrations").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM policy_day_reservations WHERE trade_date='2026-08-11'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("reason", [True, [], {}, "   ", "x" * 501])
def test_policy_override_rejects_invalid_reason_values(tmp_path, reason):
    store = ss.SentimentStore(tmp_path / "policy-override.sqlite3")
    store.reserve_policy_day("2026-08-10", "policy-v1")

    with pytest.raises(ValueError, match="reason|string|non-empty|500"):
        store.reserve_policy_day(
            "2026-08-11", "policy-v2", override_reason=reason,
        )

    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM policy_migrations").fetchone()[0] == 0


@pytest.mark.parametrize("reason", [True, [], {}])
def test_run_configured_does_not_stringify_invalid_policy_override_reason(
    tmp_path, reason,
):
    initial = _config(tmp_path)
    initial_settings = initial["sector_sentiment"]
    initial_hash = ss._validate_retrieval_policy(
        initial_settings, initial_settings["retrieval"],
    )
    ss.open_store(tmp_path).reserve_policy_day("2026-08-10", initial_hash)
    migrated = _config(tmp_path)
    migrated["sector_sentiment"]["mediacrawler_commit"] = "b" * 40
    migrated["sector_sentiment"]["policy_override_reason"] = reason
    runner_called = False

    def runner(*_args):
        nonlocal runner_called
        runner_called = True

    with pytest.raises(ValueError, match="reason|string"):
        ss.run_configured(migrated, runner=runner, trade_date="2026-08-11")

    assert runner_called is False


def test_policy_override_reason_can_authorize_only_one_migration(tmp_path):
    store = ss.SentimentStore(tmp_path / "policy-override.sqlite3")
    store.reserve_policy_day("2026-08-10", "policy-v1")
    store.reserve_policy_day(
        "2026-08-11", "policy-v2", override_reason="一次性升级采集器",
    )

    with pytest.raises(ValueError, match="override|used|consumed|frozen"):
        store.reserve_policy_day(
            "2026-08-12", "policy-v3", override_reason="一次性升级采集器",
        )

    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM policy_migrations").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM policy_day_reservations WHERE trade_date='2026-08-12'"
        ).fetchone()[0] == 0


def test_concurrent_policy_overrides_leave_one_consistent_migration(tmp_path, monkeypatch):
    path = tmp_path / "policy-override.sqlite3"
    ss.SentimentStore(path).reserve_policy_day("2026-08-10", "policy-v1")
    first = ss.SentimentStore(path)
    second = ss.SentimentStore(path)
    barrier = threading.Barrier(2)
    original = ss.SentimentStore.resolve_policy_cohort

    def synchronized_resolve(self, trade_date, config_hash, override_reason=""):
        barrier.wait(timeout=5)
        return original(self, trade_date, config_hash, override_reason)

    monkeypatch.setattr(ss.SentimentStore, "resolve_policy_cohort", synchronized_resolve)

    def reserve(store, policy):
        try:
            return ("ok", store.reserve_policy_day(
                "2026-08-11", policy, override_reason=f"迁移到 {policy}",
            ))
        except Exception as exc:  # Assert the public failure type below.
            return ("error", exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda args: reserve(*args),
            [(first, "policy-v2"), (second, "policy-v3")],
        ))

    assert [status for status, _ in results].count("ok") == 1
    errors = [value for status, value in results if status == "error"]
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "conflict" in str(errors[0]) or "reserved" in str(errors[0])
    with sqlite3.connect(path) as conn:
        reservation = conn.execute(
            "SELECT config_hash, policy_cohort_id FROM policy_day_reservations "
            "WHERE trade_date='2026-08-11'"
        ).fetchone()
        migration = conn.execute(
            "SELECT new_config_hash, new_policy_cohort_id FROM policy_migrations "
            "WHERE effective_trade_date='2026-08-11'"
        ).fetchone()
    assert reservation == migration


def test_concurrent_policy_overrides_cannot_reuse_one_reason_on_different_dates(
    tmp_path, monkeypatch,
):
    path = tmp_path / "policy-override.sqlite3"
    ss.SentimentStore(path).reserve_policy_day("2026-08-10", "policy-v1")
    first = ss.SentimentStore(path)
    second = ss.SentimentStore(path)
    barrier = threading.Barrier(2)
    original = ss.SentimentStore.resolve_policy_cohort

    def synchronized_resolve(self, trade_date, config_hash, override_reason=""):
        result = original(self, trade_date, config_hash, override_reason)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(ss.SentimentStore, "resolve_policy_cohort", synchronized_resolve)

    def reserve(store, trade_date, policy):
        try:
            return ("ok", store.reserve_policy_day(
                trade_date, policy, override_reason="同一个一次性迁移授权",
            ))
        except Exception as exc:  # Assert the public failure type below.
            return ("error", exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda args: reserve(*args),
            [
                (first, "2026-08-11", "policy-v2"),
                (second, "2026-08-12", "policy-v3"),
            ],
        ))

    assert [status for status, _ in results].count("ok") == 1
    errors = [value for status, value in results if status == "error"]
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "used" in str(errors[0]) or "consumed" in str(errors[0])
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM policy_migrations "
            "WHERE reason='同一个一次性迁移授权'"
        ).fetchone()[0] == 1


def test_natural_policy_rollover_rechecks_latest_cohort_after_obtaining_write_lock(
    tmp_path, monkeypatch,
):
    path = tmp_path / "policy-override.sqlite3"
    seed = ss.SentimentStore(path)
    start = date(2026, 1, 1)
    for offset in range(60):
        seed.reserve_policy_day(
            (start + timedelta(days=offset)).isoformat(), "policy-v1",
        )
    store = ss.SentimentStore(path)
    original = ss.SentimentStore.resolve_policy_cohort
    competing = original(store, "2026-03-02", "policy-v3")
    injected = False

    def synchronized_resolve(self, trade_date, config_hash, override_reason=""):
        nonlocal injected
        result = original(self, trade_date, config_hash, override_reason)
        if not injected:
            injected = True
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO policy_day_reservations
                       (trade_date, config_hash, policy_cohort_id, cohort_start_date,
                        status, reserved_at)
                       VALUES ('2026-03-02', 'policy-v3', ?, ?, 'running',
                               '2026-03-02T00:00:00+08:00')""",
                    (competing["policy_cohort_id"], competing["cohort_start_date"]),
                )
        return result

    monkeypatch.setattr(ss.SentimentStore, "resolve_policy_cohort", synchronized_resolve)

    with pytest.raises(ValueError, match="changed|conflict"):
        store.reserve_policy_day("2026-03-03", "policy-v2")

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT trade_date, config_hash FROM policy_day_reservations "
            "WHERE trade_date>='2026-03-02' ORDER BY trade_date"
        ).fetchall()
    assert rows == [("2026-03-02", "policy-v3")]


def test_policy_override_reason_is_audit_data_not_retrieval_policy(tmp_path):
    first = _config(tmp_path)
    first["sector_sentiment"]["policy_override_reason"] = "升级 MediaCrawler"
    second = _config(tmp_path)
    second["sector_sentiment"]["policy_override_reason"] = "降低采集配额"

    assert ss._validate_retrieval_policy(
        first["sector_sentiment"], first["sector_sentiment"]["retrieval"],
    ) == ss._validate_retrieval_policy(
        second["sector_sentiment"], second["sector_sentiment"]["retrieval"],
    )


def test_run_configured_uses_policy_override_reason_for_the_precollection_reservation(tmp_path):
    initial = _config(tmp_path)
    initial_settings = initial["sector_sentiment"]
    initial_hash = ss._validate_retrieval_policy(
        initial_settings, initial_settings["retrieval"],
    )
    ss.open_store(tmp_path).reserve_policy_day("2026-08-10", initial_hash)
    migrated = _config(tmp_path)
    migrated["sector_sentiment"]["mediacrawler_commit"] = "b" * 40
    migrated["sector_sentiment"]["policy_override_reason"] = "升级 MediaCrawler"

    report = ss.run_configured(
        migrated,
        runner=lambda _job, destination, _timeout, _limits: destination.write_text(""),
        trade_date="2026-08-11",
    )

    assert report["collection"]["trade_date"] == "2026-08-11"
    with sqlite3.connect(ss.open_store(tmp_path).path) as conn:
        migration = conn.execute(
            "SELECT effective_trade_date, reason FROM policy_migrations"
        ).fetchone()
    assert migration == ("2026-08-11", "升级 MediaCrawler")


def test_policy_day_is_reserved_before_ingest_side_effects_and_survives_crash(
    tmp_path, monkeypatch,
):
    base_cfg = _config(tmp_path)
    base_settings = base_cfg["sector_sentiment"]
    base_hash = ss._validate_retrieval_policy(
        base_settings, base_settings["retrieval"],
    )
    store = ss.open_store(tmp_path)
    start = date(2026, 1, 1)
    for offset in range(60):
        store.record_collection({
            "trade_date": (start + timedelta(days=offset)).isoformat(),
            "coverage": 1.0, "search_coverage": 1.0, "creator_coverage": 1.0,
            "platforms": {}, "funnel": {}, "config_hash": base_hash,
        })

    policy_b = _config(tmp_path)
    policy_b["sector_sentiment"]["mediacrawler_commit"] = "b" * 40
    policy_b_hash = ss._validate_retrieval_policy(
        policy_b["sector_sentiment"], policy_b["sector_sentiment"]["retrieval"],
    )
    policy_c = _config(tmp_path)
    policy_c["sector_sentiment"]["mediacrawler_commit"] = "c" * 40
    policy_c_hash = ss._validate_retrieval_policy(
        policy_c["sector_sentiment"], policy_c["sector_sentiment"]["retrieval"],
    )
    run_day = (start + timedelta(days=60)).isoformat()
    runner_calls = []

    def runner(job, destination, _timeout, _limits):
        runner_calls.append(job.config_hash)
        destination.write_text(json.dumps(
            _item("partial-b", "机器人板块资金流入"), ensure_ascii=False,
        ) + "\n")

    original_discover = ss.SentimentStore.discover_creator_candidates

    def crash_after_ingest(self, records, settings):
        if settings.get("config_hash") == policy_b_hash:
            raise RuntimeError("simulated crash after search ingest")
        return original_discover(self, records, settings)

    monkeypatch.setattr(ss.SentimentStore, "discover_creator_candidates", crash_after_ingest)
    with pytest.raises(RuntimeError, match="simulated crash"):
        ss.run_configured(policy_b, runner=runner, trade_date=run_day)

    assert ss.open_store(tmp_path).list_content(run_day)[0]["content_id"] == "partial-b"
    with pytest.raises(ValueError, match="reserved|frozen|conflict"):
        ss.run_configured(policy_c, runner=runner, trade_date=run_day)
    assert runner_calls == [policy_b_hash]
    with sqlite3.connect(store.path) as conn:
        reserved_hash = conn.execute(
            "SELECT config_hash FROM policy_day_reservations WHERE trade_date=?", (run_day,),
        ).fetchone()[0]
    assert reserved_hash == policy_b_hash
    assert reserved_hash != policy_c_hash


def test_frozen_policy_hash_is_persisted_on_runs_creator_evidence_and_events(tmp_path):
    def runner(job, destination, _timeout, _limits):
        destination.write_text(json.dumps(_item(
            "policy", "机器人板块资金流入",
            author_id="up-1", author_name="财经小王",
        ), ensure_ascii=False) + "\n")

    cfg = _config(tmp_path, candidate_min_contents=1)
    ss.run_configured(cfg, runner=runner, trade_date="2026-08-10")
    store = ss.open_store(tmp_path)
    creator = store.list_creators("candidate")[0]
    store.moderate_creator("bili", "up-1", "approve")
    run = store.collection_summary("2026-08-10")
    event = store.creator_events("bili", "up-1")[-1]

    assert len(run["config_hash"]) == 64
    assert run["query_version"] == "sector-finance-query-v1"
    assert run["relevance_version"] == "sector-finance-relevance-v1"
    assert run["creator_rule_version"] == "sector-finance-creator-v1"
    assert run["semantic_prompt_version"] == "sector-semantic-v1"
    assert creator["config_hash"] == run["config_hash"]
    assert creator["creator_rule_version"] == "sector-finance-creator-v1"
    assert all("content_id" not in evidence for evidence in creator["evidence"])
    assert creator["evidence"][0]["query_version"] == "sector-finance-query-v1"
    assert creator["evidence"][0]["relevance_version"] == "sector-finance-relevance-v1"
    assert event["config_hash"] == run["config_hash"]
    assert event["creator_rule_version"] == "sector-finance-creator-v1"


def test_legacy_collection_migration_preserves_coverage_null_funnel_and_latest_rowid(tmp_path, monkeypatch):
    db = tmp_path / "sentiment.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE collection_runs (
            run_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL, coverage REAL NOT NULL,
            platforms_json TEXT NOT NULL, status TEXT NOT NULL, completed_at TEXT NOT NULL
        )""")
        conn.execute(
            "INSERT INTO collection_runs VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy", "2026-08-09", 0.75, '{"bili":{"status":"ok"}}', "degraded", "2026-08-09T10:00:00+00:00"),
        )
    store = ss.SentimentStore(db)

    legacy = store.collection_summary("2026-08-09")
    assert legacy["coverage"] == 0.75
    assert legacy["search_coverage"] == 0.75
    assert legacy["creator_coverage"] == 0.75
    assert legacy["funnel"] is None

    monkeypatch.setattr(ss, "_now", lambda: "2026-08-10T10:00:00+00:00")
    for coverage in (0.25, 0.5):
        store.record_collection({
            "trade_date": "2026-08-10", "coverage": coverage,
            "search_coverage": coverage, "creator_coverage": 1.0,
            "platforms": {}, "funnel": {},
            "query_version": "sector-finance-query-v1",
            "relevance_version": "sector-finance-relevance-v1",
            "creator_rule_version": "sector-finance-creator-v1",
            "semantic_prompt_version": "sector-semantic-v1",
            "config_hash": "policy-hash",
        })
    assert store.collection_summary("2026-08-10")["coverage"] == 0.5

    reopened = ss.SentimentStore(db)
    current = reopened.collection_summary("2026-08-10")
    assert current["search_coverage"] == 0.5
    assert current["creator_coverage"] == 1.0


def test_creator_metrics_use_all_unique_content_duplicate_is_time_stable_and_evidence_is_redacted(
    tmp_path, monkeypatch,
):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    settings = _retrieval_settings(candidate_min_contents=1, config_hash="policy-hash")
    accepted = {
        **_item("accepted", "联系 test@example.com，手机13800138000，微信wx_secret，@finance_guy"),
        "author_id": "up-1", "author_name": "财经小王", "relevance_score": 0.9,
        "relevance_decision": "accepted", "relevance_reasons": ["path:entity_and_finance"],
        "sector_ids": ["concept:robot"],
    }
    filtered = {
        **_item("filtered", "周末随拍"), "author_id": "up-1", "author_name": "财经小王",
        "relevance_score": 0.1, "relevance_decision": "filtered_non_financial",
        "relevance_reasons": ["audit:no_sector_or_finance_match"], "sector_ids": [],
    }
    monkeypatch.setattr(ss, "_now", lambda: "2026-08-10T10:00:00+00:00")
    assert store.discover_creator_candidates([accepted, filtered], settings) == {
        "created": 1, "updated": 0,
    }
    before = store.list_creators()[0]
    monkeypatch.setattr(ss, "_now", lambda: "2026-08-11T10:00:00+00:00")
    assert store.discover_creator_candidates([accepted, filtered], settings) == {
        "created": 0, "updated": 0,
    }
    after = store.list_creators()[0]

    assert after["last_discovered_at"] == before["last_discovered_at"]
    assert after["valid_content_count"] == 1
    assert after["total_content_count"] == 2
    assert after["financial_ratio"] == pytest.approx(0.5)
    evidence = after["evidence"][0]
    assert "content_id" not in evidence
    assert "example.com" not in evidence["text"]
    assert "13800138000" not in evidence["text"]
    assert "wx_secret" not in evidence["text"]
    assert "finance_guy" not in evidence["text"]


def test_creator_evidence_is_immutable_and_summary_uses_latest_policy_provenance(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")
    first = {
        **_item("first", "机器人板块资金流入"), "author_id": "opaque",
        "relevance_score": 0.9, "relevance_decision": "accepted",
        "relevance_reasons": ["path:entity_and_finance"],
        "sector_ids": ["concept:robot"],
    }
    first_settings = _retrieval_settings(
        candidate_min_contents=1, config_hash="policy-v1",
    )
    second_settings = _retrieval_settings(
        candidate_min_contents=1, config_hash="policy-v2",
    )
    store.discover_creator_candidates([first], first_settings)

    # Replaying an audited evidence key under a later cohort cannot rewrite it.
    store.discover_creator_candidates([{
        **first, "text": "被新策略重写的文本", "relevance_score": 0.1,
        "relevance_decision": "filtered_non_financial",
        "relevance_reasons": ["new-policy-reason"], "sector_ids": ["concept:new"],
    }], second_settings)
    store.discover_creator_candidates([{
        **_item("second", "机器人产业链订单增长"), "author_id": "opaque",
        "relevance_score": 0.95, "relevance_decision": "accepted",
        "relevance_reasons": ["path:entity_and_finance"],
        "sector_ids": ["concept:robot"],
    }], second_settings)
    store.moderate_creator("bili", "opaque", "approve")

    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        evidence_rows = conn.execute(
            """SELECT content_id, evidence_text, relevance_score, relevance_decision,
                      relevance_reasons_json, sector_ids_json, config_hash
               FROM creator_content ORDER BY rowid"""
        ).fetchall()
    creator = store.list_creators()[0]
    event = store.creator_events("bili", "opaque")[0]

    assert [dict(row) for row in evidence_rows] == [
        {
            "content_id": "first", "evidence_text": "机器人板块资金流入",
            "relevance_score": 0.9, "relevance_decision": "accepted",
            "relevance_reasons_json": '["path:entity_and_finance"]',
            "sector_ids_json": '["concept:robot"]', "config_hash": "policy-v1",
        },
        {
            "content_id": "second", "evidence_text": "机器人产业链订单增长",
            "relevance_score": 0.95, "relevance_decision": "accepted",
            "relevance_reasons_json": '["path:entity_and_finance"]',
            "sector_ids_json": '["concept:robot"]', "config_hash": "policy-v2",
        },
    ]
    assert creator["config_hash"] == "policy-v2"
    assert {item["config_hash"] for item in creator["evidence"]} == {
        "policy-v1", "policy-v2",
    }
    assert event["config_hash"] == "policy-v2"


def test_legacy_persisted_evidence_is_minimized_and_redacted_on_upgrade(tmp_path):
    db = tmp_path / "sentiment.sqlite3"
    store = ss.SentimentStore(db)
    canonical = _item("legacy", "机器人板块资金流入")
    canonical["trade_date"] = "2026-08-10"
    store.ingest([canonical])
    store.discover_creator_candidates([{
        **_item("legacy", "机器人板块资金流入"),
        "author_id": "opaque", "relevance_score": 0.9,
        "relevance_decision": "accepted", "sector_ids": ["concept:robot"],
    }], _retrieval_settings(candidate_min_contents=1))
    store.save_daily_score({
        "trade_date": "2026-08-10", "sector_id": "concept:robot",
        "sector_name": "机器人", "taxonomy": "concept", "platforms": ["bili"],
        "independent_authors": 1, "mapping_confidence": 0.9,
        "sentiment_extreme": 0.8, "attention_acceleration": 0.8,
        "consensus_crowding": 0.8, "market_divergence": 0.1,
        "short_risk": 50, "swing_risk": 40,
        "evidence": [{
            "content_id": "private-id", "platform": "bili", "stance": 1,
            "text": "邮箱old@example.com 电话010-12345678 微信：财经小王",
        }],
    })
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE creators SET evidence_json=?",
            (json.dumps([{
                "content_id": "private-id", "text": "公众号：机器人研究所",
                "relevance_score": 0.9,
            }], ensure_ascii=False),),
        )
        conn.execute(
            """UPDATE content SET text=?, semantic_text=?, author_name=?""",
            ("联系+86 138-0013-8000", "微博：https://weibo.com/private", "@财经小王"),
        )
        # Model a database created before the one-shot privacy migration existed.
        conn.execute(
            "DELETE FROM schema_migrations WHERE name='sector_evidence_privacy_v1'"
        )

    reopened = ss.SentimentStore(db)
    with sqlite3.connect(db) as conn:
        creator_json = conn.execute("SELECT evidence_json FROM creators").fetchone()[0]
        score_json = conn.execute("SELECT evidence_json FROM daily_scores").fetchone()[0]
        content = conn.execute(
            "SELECT text, semantic_text, author_name FROM content"
        ).fetchone()

    persisted = creator_json + score_json + "".join(content)
    assert "content_id" not in creator_json
    assert "private-id" not in persisted
    assert "old@example.com" not in persisted
    assert "010-12345678" not in persisted
    assert "机器人研究所" not in persisted
    assert "138-0013-8000" not in persisted
    assert "weibo.com/private" not in persisted
    assert "财经小王" not in persisted
    assert "[" in persisted
    assert reopened.scores_for_date("2026-08-10")[0]["evidence"] == [{
        "platform": "bili", "text": "邮箱[邮箱已脱敏] 电话[电话已脱敏] 微信[账号已脱敏]",
        "stance": 1,
    }]

    traced: list[str] = []
    original_connect = ss.sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(traced.append)
        return connection

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ss.sqlite3, "connect", traced_connect)
    try:
        ss.SentimentStore(db)
    finally:
        monkeypatch.undo()
    scrub_prefixes = (
        "UPDATE content SET text=", "UPDATE creator_content SET evidence_text=",
        "UPDATE creators SET display_name=", "UPDATE daily_scores SET evidence_json=",
    )
    assert not any(statement.strip().startswith(scrub_prefixes) for statement in traced)


def test_missing_creator_raises_domain_error(tmp_path):
    store = ss.SentimentStore(tmp_path / "sentiment.sqlite3")

    with pytest.raises(ss.CreatorNotFoundError):
        store.moderate_creator("bili", "missing", "approve")
