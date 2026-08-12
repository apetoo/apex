from __future__ import annotations

import json
from pathlib import Path

import pytest

from apex.eastmoney_guba import (
    BlockedResponse,
    EastmoneyExporter,
    EastmoneyParser,
    EastmoneyRunner,
    EastmoneyTargetProvider,
    QuotaBudget,
    SchemaChanged,
)


FIXTURES = Path(__file__).parent / "fixtures" / "eastmoney"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_target_provider_requires_explicit_forum_and_selects_top_turnover_constituents():
    provider = EastmoneyTargetProvider(constituent_count=2, pool_version="pool-v1")
    targets, missing = provider.build_targets(
        [
            {"sector_id": "robot", "sector_name": "机器人", "eastmoney_forum_id": "bk0910"},
            {"sector_id": "drug", "sector_name": "创新药"},
        ],
        {
            "robot": [
                {"stock_code": "000001", "turnover_20d": 10},
                {"stock_code": "000002", "turnover_20d": 30},
                {"stock_code": "000003", "turnover_20d": 20},
            ]
        },
        generated_at="2026-08-12T16:00:00+08:00",
    )

    assert [(t.source_type, t.forum_id) for t in targets] == [
        ("sector_forum", "bk0910"),
        ("constituent_forum", "000002"),
        ("constituent_forum", "000003"),
    ]
    assert missing == [{"sector_id": "drug", "reason": "missing_explicit_forum_id"}]
    assert all(t.pool_version == "pool-v1" for t in targets)


def test_parser_prefers_json_and_keeps_only_first_level_comments():
    parser = EastmoneyParser()
    posts = parser.parse_posts(fixture("posts.json"), "application/json")
    comments = parser.parse_comments(fixture("comments.json"), "application/json")

    assert posts[0]["content_id"] == "1001"
    assert posts[0]["title"] == "机器人板块放量"
    assert [item["comment_id"] for item in comments] == ["c-1"]


def test_parser_uses_html_fallback_but_rejects_block_and_schema_changes():
    parser = EastmoneyParser()
    posts = parser.parse_posts(fixture("posts.html"), "text/html")
    assert posts[0]["content_id"] == "2001"

    with pytest.raises(BlockedResponse):
        parser.parse_posts("<html>请输入验证码</html>".encode(), "text/html")
    with pytest.raises(SchemaChanged):
        parser.parse_posts(json.dumps({"unexpected": []}).encode(), "application/json")
    with pytest.raises(SchemaChanged):
        parser.parse_posts(b"<html><body>normal page, new markup</body></html>", "text/html")


def test_parser_uses_html_fallback_for_first_level_comments():
    comments = EastmoneyParser().parse_comments(fixture("comments.html"), "text/html")

    assert comments == [{
        "content_id": "2001", "comment_id": "hc-1", "parent_content_id": "2001",
        "title": "", "text": "行情已经很拥挤", "published_at": "2026-08-11 15:10:00",
        "author_id": "html-user", "read_count": 0, "reply_count": 0, "like_count": 4,
    }]


def test_exporter_emits_ingestible_record_without_identity(tmp_path: Path):
    output = tmp_path / "records.jsonl"
    exporter = EastmoneyExporter(output, author_salt="local-secret")
    exporter.export(
        [{
            "content_id": "1001", "comment_id": "", "title": "标题", "text": "正文",
            "published_at": "2026-08-11T15:20:00+08:00", "author_id": "private-user",
            "read_count": 100, "reply_count": 2, "like_count": 3,
        }],
        trade_date="2026-08-12", batch_id="batch-1", sector_id="robot",
        source_type="sector_forum", stock_code=None, pool_version="pool-v1",
        collected_at="2026-08-12T16:00:00+08:00",
    )
    record = json.loads(output.read_text().strip())

    assert record["platform"] == "eastmoney"
    assert record["engagement"] == 105
    assert record["sector_ids"] == ["robot"]
    assert record["source_type"] == "sector_forum"
    assert record["author_hash"]
    assert "private-user" not in output.read_text()
    assert "author_id" not in record and "author_name" not in record


def test_quota_and_runner_return_explicit_partial_and_empty_states(tmp_path: Path):
    quota = QuotaBudget(max_requests=2)
    assert quota.consume() is True
    assert quota.consume() is True
    assert quota.consume() is False
    assert quota.exhausted is True

    partial = EastmoneyRunner.classify_result(
        {"records": 3, "request_count": 2, "failed_targets": 1, "quota_exhausted": True}
    )
    empty = EastmoneyRunner.classify_result(
        {"records": 0, "request_count": 2, "failed_targets": 0, "quota_exhausted": False}
    )
    assert partial.status == "partial" and partial.quota_exhausted is True
    assert empty.status == "empty_valid"


@pytest.mark.parametrize(
    ("reason", "status"),
    [("blocked", "blocked"), ("schema_changed", "schema_changed"), ("failed", "failed")],
)
def test_runner_preserves_terminal_failure_reason(reason: str, status: str):
    result = EastmoneyRunner.classify_result(
        {"records": 0, "request_count": 1, "failed_targets": 1, "terminal_reason": reason}
    )
    assert result.status == status
