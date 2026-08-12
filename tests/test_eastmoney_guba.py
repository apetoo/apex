from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class _CollectorHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, str, bytes]] = {}

    def do_GET(self):
        status, content_type, body = self.routes[self.path]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


@pytest.fixture
def collector_server():
    _CollectorHandler.routes = {
        "/list": (200, "application/json", fixture("posts.json")),
        "/detail/1001": (200, "application/json", fixture("posts.json")),
        "/comments/1001": (200, "application/json", fixture("comments.json")),
        "/blocked": (403, "text/html", b"forbidden"),
        "/captcha": (200, "text/html; charset=utf-8", "<html>请输入验证码</html>".encode()),
        "/schema": (200, "application/json", b'{"changed": []}'),
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CollectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def _manifest(tmp_path: Path, base: str, path: str = "/list", max_requests: int = 10):
    return {
        "trade_date": "2026-08-12", "batch_id": "batch-process",
        "collected_at": "2026-08-12T16:00:00+08:00",
        "output_file": str(tmp_path / "records.jsonl"), "author_salt": "test-salt",
        "jobdir": str(tmp_path / "jobdir"),
        "settings": {"max_requests": max_requests, "requests_per_target": 10,
                     "posts_per_target": 10, "comments_per_post": 10,
                     "download_delay_seconds": 0, "timeout_seconds": 3, "retry_times": 1},
        "jobs": [{
            "kind": "list", "url": base + path, "target_id": "bk0910",
            "sector_id": "robot", "source_type": "sector_forum", "stock_code": None,
            "pool_version": "pool-v1", "detail_url_template": base + "/detail/{content_id}",
            "comments_url_template": base + "/comments/{content_id}",
        }],
    }


def _run_manifest(tmp_path: Path, manifest: dict):
    job_file = tmp_path / "manifest.json"
    report_file = tmp_path / "report.json"
    job_file.write_text(json.dumps(manifest), encoding="utf-8")
    return EastmoneyRunner().run(job_file, report_file, timeout_seconds=15), report_file


def test_process_path_exports_detail_and_first_level_comments(tmp_path: Path, collector_server: str):
    result, report_file = _run_manifest(tmp_path, _manifest(tmp_path, collector_server))
    records = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]

    assert result.status == "ok"
    assert {(r["content_id"], r["comment_id"]) for r in records} == {("1001", ""), ("1001", "c-1")}
    assert all(r["platform"] == "eastmoney" for r in records)
    assert json.loads(report_file.read_text())["request_count"] == 3


@pytest.mark.parametrize(("path", "status"), [
    ("/blocked", "blocked"), ("/captcha", "blocked"), ("/schema", "schema_changed"),
])
def test_process_path_classifies_block_and_schema(tmp_path: Path, collector_server: str,
                                                  path: str, status: str):
    result, _ = _run_manifest(tmp_path, _manifest(tmp_path, collector_server, path=path))
    assert result.status == status


def test_process_path_enforces_total_request_quota(tmp_path: Path, collector_server: str):
    result, _ = _run_manifest(tmp_path, _manifest(tmp_path, collector_server, max_requests=1))
    assert result.status == "partial"
    assert result.quota_exhausted is True


def test_runner_never_reuses_stale_or_malformed_report(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text('{"records": 99, "request_count": 1, "failed_targets": 0}', encoding="utf-8")
    missing_manifest = tmp_path / "missing.json"
    result = EastmoneyRunner().run(missing_manifest, report, timeout_seconds=5)
    assert result.status == "failed" and result.records == 0
    assert not report.exists()

    with pytest.raises(ValueError):
        EastmoneyRunner.classify_result({"records": "not-an-int"})
