from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from scrapy.exceptions import CloseSpider
from scrapy.http import HtmlResponse

from apex.eastmoney_guba import (
    BlockedResponse,
    EastmoneyExporter,
    EastmoneyParser,
    EastmoneyRunner,
    EastmoneyTargetProvider,
    QuotaBudget,
    SchemaChanged,
)
from apex.eastmoney_guba.spider import (
    EastmoneySpider,
    ExponentialRetryMiddleware,
    _validate_manifest,
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
            "url": "https://guba.eastmoney.com/news,bk0910,1001.html",
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


def test_json_post_and_comment_export_canonical_public_detail_urls(tmp_path: Path):
    post_payload = json.loads(fixture("posts.json"))
    comment_payload = json.loads(fixture("comments.json"))
    detail_url = "https://guba.eastmoney.com/news,bk0910,1001.html"
    post_payload["re"][0]["post_url"] = detail_url
    comment_payload["re"][0]["post_url"] = detail_url

    parser = EastmoneyParser()
    posts = parser.parse_posts(json.dumps(post_payload).encode(), "application/json")
    comments = parser.parse_comments(json.dumps(comment_payload).encode(), "application/json")
    output = tmp_path / "records.jsonl"
    EastmoneyExporter(output, author_salt="local-secret").export(
        posts + comments,
        trade_date="2026-08-12", batch_id="batch-urls", sector_id="robot",
        source_type="sector_forum", stock_code=None, pool_version="pool-v1",
        collected_at="2026-08-12T16:00:00+08:00",
    )

    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [(record["content_id"], record["comment_id"], record["url"])
            for record in records] == [
                ("1001", "", detail_url),
                ("1001", "c-1", detail_url),
            ]


def test_parser_preserves_post_last_activity_timestamp():
    payload = json.loads(fixture("posts.json"))
    payload["re"][0]["post_last_time"] = "2026-08-11T16:00:00+08:00"

    posts = EastmoneyParser().parse_posts(json.dumps(payload).encode(), "application/json")

    assert posts[0]["last_activity_at"] == "2026-08-11T16:00:00+08:00"


def test_html_post_keeps_independent_published_and_last_activity_timestamps():
    """Catches HTML parsing that mistakes the list's final-update field for publish time."""
    posts = EastmoneyParser().parse_posts(
        b'''<div class="articleh" data-postid="old-1001" data-publish-time="2026-08-11T13:59:00+08:00">
          <a class="l3" href="/news,bk0910,old-1001.html">old post</a>
          <span class="l5 a5">2026-08-11T14:10:00+08:00</span>
        </div>''',
        "text/html",
    )

    assert posts[0]["published_at"] == "2026-08-11T13:59:00+08:00"
    assert posts[0]["last_activity_at"] == "2026-08-11T14:10:00+08:00"


@pytest.mark.parametrize("url", [
    None,
    "https://guba.eastmoney.com/list,bk0910.html",
    "https://guba.eastmoney.com/news,bk0910,1001,extra.html",
    "https://guba.eastmoney.com/news,bk0910%2C1001.html",
    "https://guba.eastmoney.com/news,bk0910,%2F1001.html",
    "https://guba.eastmoney.com/news,bk0910,../1001.html",
    "https://guba.eastmoney.com/news,bk0910,1001.html?next=%2Flist",
    "https://guba.eastmoney.com/news,bk0910,1001.html#comments",
])
def test_exporter_rejects_records_without_a_canonical_public_detail_url(tmp_path: Path, url):
    with pytest.raises(ValueError, match="canonical.*detail.*URL"):
        EastmoneyExporter(tmp_path / "records.jsonl", author_salt="local-secret").export(
            [{
                "content_id": "1001", "comment_id": "", "title": "标题", "text": "正文",
                "published_at": "2026-08-11T15:20:00+08:00", "url": url,
            }],
            trade_date="2026-08-12", batch_id="batch-url-validation", sector_id="robot",
            source_type="sector_forum", stock_code=None, pool_version="pool-v1",
            collected_at="2026-08-12T16:00:00+08:00",
        )


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


@pytest.mark.parametrize("value", [True, "2", 1.5, -1])
def test_quota_budget_strictly_rejects_invalid_constructor_values(value):
    with pytest.raises(ValueError):
        QuotaBudget(value)


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
    hits: dict[str, int] = {}

    def do_GET(self):
        self.hits[self.path] = self.hits.get(self.path, 0) + 1
        status, content_type, body = self.routes[self.path]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


@pytest.fixture
def collector_server(monkeypatch):
    monkeypatch.setenv("APEX_EASTMONEY_TESTING", "1")
    page_one = {"re": [{"comment_id": f"p1-{index}", "post_id": "1001",
                         "comment_content": "看多", "comment_publish_time": "2026-08-11 16:00:00",
                         "comment_like_count": 0, "user_id": f"u{index}",
                         "reply_to_comment_id": ""} for index in range(30)],
                "has_more": True, "next_cursor": "two"}
    page_two = {"re": [{"comment_id": f"p1-{index}", "post_id": "1001",
                         "comment_content": "重复", "comment_publish_time": "2026-08-11 16:10:00",
                         "comment_like_count": 0, "user_id": f"u{index}",
                         "reply_to_comment_id": ""} for index in range(10)] +
                       [{"comment_id": f"p2-{index}", "post_id": "1001",
                         "comment_content": "谨慎", "comment_publish_time": "2026-08-11 16:10:00",
                         "comment_like_count": 0, "user_id": f"v{index}",
                         "reply_to_comment_id": ""} for index in range(20)],
                "has_more": True, "next_cursor": "three"}
    _CollectorHandler.hits = {}
    _CollectorHandler.routes = {
        "/list": (200, "application/json", fixture("posts.json")),
        "/detail/1001": (200, "application/json", fixture("posts.json")),
        "/comments/1001": (200, "application/json", fixture("comments.json")),
        "/blocked": (403, "text/html", b"forbidden"),
        "/captcha": (200, "text/html; charset=utf-8", "<html>请输入验证码</html>".encode()),
        "/schema": (200, "application/json", b'{"changed": []}'),
        "/comment-pages/1001?cursor=one": (200, "application/json", json.dumps(page_one).encode()),
        "/comment-pages/1001?cursor=two": (200, "application/json", json.dumps(page_two).encode()),
        "/server-error": (500, "application/json", b'{"error": true}'),
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CollectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def _manifest(tmp_path: Path, base: str, path: str = "/list", max_requests: int = 10,
              settings_override: dict | None = None):
    settings = {"max_requests": max_requests, "requests_per_target": 10,
                "posts_per_target": 10, "comments_per_post": 10,
                "download_delay_seconds": 0, "timeout_seconds": 3, "retry_times": 1}
    settings.update(settings_override or {})
    return {
        "trade_date": "2026-08-12", "batch_id": "batch-process",
        "collected_at": "2026-08-12T16:00:00+08:00",
        "output_file": str(tmp_path / "records.jsonl"), "author_salt": "test-salt",
        "jobdir": str(tmp_path / "jobdir"),
        "test_hosts": ["127.0.0.1"],
        "settings": settings,
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
    assert all(r["url"] == "https://guba.eastmoney.com/news,bk0910,1001.html" for r in records)
    assert json.loads(report_file.read_text())["request_count"] == 3


@pytest.mark.parametrize(("path", "status", "override"), [
    ("/blocked", "blocked", None), ("/captcha", "blocked", None),
    ("/schema", "schema_changed", {"circuit_breaker_failures": 1}),
])
def test_process_path_classifies_block_and_schema(tmp_path: Path, collector_server: str,
                                                  path: str, status: str,
                                                  override: dict | None):
    result, _ = _run_manifest(tmp_path, _manifest(
        tmp_path, collector_server, path=path, settings_override=override,
    ))
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


def test_manifest_rejects_non_https_or_non_eastmoney_hosts_without_test_switch(tmp_path: Path,
                                                                               monkeypatch):
    monkeypatch.delenv("APEX_EASTMONEY_TESTING", raising=False)
    base = _manifest(tmp_path, "http://evil.example")
    base.pop("test_hosts")
    with pytest.raises(ValueError, match="HTTPS Eastmoney"):
        _validate_manifest(base)

    injected = _manifest(tmp_path, "http://127.0.0.1")
    with pytest.raises(ValueError, match="APEX_EASTMONEY_TESTING"):
        _validate_manifest(injected)
    base["jobs"][0]["url"] = "https://evil.example/list"
    with pytest.raises(ValueError, match="HTTPS Eastmoney"):
        _validate_manifest(base)


def test_test_host_injection_only_allows_loopback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APEX_EASTMONEY_TESTING", "1")
    manifest = _manifest(tmp_path, "http://evil.example")
    manifest["test_hosts"] = ["evil.example"]
    with pytest.raises(ValueError, match="loopback"):
        _validate_manifest(manifest)


def test_retry_backoff_is_bounded_exponential():
    assert [ExponentialRetryMiddleware.delay_for(n, base=0.5, cap=2) for n in range(1, 6)] == [
        0.5, 1.0, 2.0, 2.0, 2.0,
    ]


def test_manifest_rejects_bool_fractional_negative_and_unknown_budget_values(tmp_path: Path,
                                                                            monkeypatch):
    monkeypatch.setenv("APEX_EASTMONEY_TESTING", "1")
    collector_server = "http://127.0.0.1"
    for value in (True, 1.5, -1, "10"):
        manifest = _manifest(tmp_path, collector_server)
        manifest["settings"]["max_requests"] = value
        with pytest.raises(ValueError, match="max_requests"):
            _validate_manifest(manifest)
    manifest = _manifest(tmp_path, collector_server, max_requests=0)
    assert _validate_manifest(manifest)["settings"]["max_requests"] == 0


def test_known_empty_html_contract_is_valid_empty():
    parser = EastmoneyParser()
    assert parser.parse_posts(b'<div class="articlelist"></div>', "text/html") == []
    assert parser.parse_comments(b'<div class="comment_list"></div>', "text/html") == []


def test_comment_page_exposes_typed_cursor_and_filters_window():
    payload = json.loads(fixture("comments.json"))
    payload["has_more"] = True
    payload["next_cursor"] = "cursor-2"
    page = EastmoneyParser().parse_comment_page(
        json.dumps(payload).encode(), "application/json",
        window_start="2026-08-11 15:30:00",
    )
    assert [record["comment_id"] for record in page.records] == ["c-1"]
    assert page.next_cursor == "cursor-2" and page.has_more is True


def test_process_comment_pagination_stops_at_fifty(tmp_path: Path, collector_server: str):
    manifest = _manifest(tmp_path, collector_server)
    manifest["settings"]["comments_per_post"] = 50
    job = manifest["jobs"][0]
    job["comments_url_template"] = collector_server + "/comment-pages/{content_id}?cursor=one"
    job["comments_next_url_template"] = collector_server + "/comment-pages/{content_id}?cursor={cursor}"
    result, report = _run_manifest(tmp_path, manifest)
    records = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]
    assert result.status == "ok"
    assert sum(bool(record["comment_id"]) for record in records) == 50
    assert json.loads(report.read_text())["request_count"] == 4


def test_list_response_schedules_page_two_and_only_comments_for_an_old_active_post(tmp_path: Path):
    detail_url = "https://guba.eastmoney.com/news,bk0910,old-1001.html"
    old_post = {
        "post_id": "old-1001", "post_title": "旧帖子", "post_content": "过期正文",
        "post_publish_time": "2026-08-11T13:59:00+08:00",
        "post_last_time": "2026-08-11T14:10:00+08:00",
        "post_click_count": 999, "post_comment_count": 1, "post_like_count": 88,
        "user_id": "old-author", "post_type": 0, "post_url": detail_url,
    }
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html")
    manifest.pop("test_hosts")
    job = manifest["jobs"][0]
    job.update({
        "window_start": "2026-08-11T14:00:00+08:00",
        "list_next_url_template": "https://guba.eastmoney.com/list,bk0910_{page}.html",
        "page": 1,
    })
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    response = HtmlResponse(
        url=job["url"], body=json.dumps({"re": [old_post]}).encode(), encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    )

    requests = list(spider.parse_job(response, job))

    assert [(request.url, request.cb_kwargs["job"]["kind"])
            for request in requests] == [
                ("https://guba.eastmoney.com/list,bk0910_2.html", "list"),
                ("https://guba.eastmoney.com/comments/old-1001", "comments"),
            ]
    assert not (tmp_path / "records.jsonl").exists()


def test_html_old_body_with_new_activity_schedules_comments_not_body_and_paginates(tmp_path: Path):
    """Catches the production HTML list path dropping an old post's in-window comments."""
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html")
    manifest.pop("test_hosts")
    job = manifest["jobs"][0]
    job.update({
        "window_start": "2026-08-11T14:00:00+08:00",
        "list_next_url_template": "https://guba.eastmoney.com/list,bk0910_{page}.html",
        "page": 1,
    })
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    response = HtmlResponse(
        url=job["url"], encoding="utf-8", headers={b"Content-Type": b"text/html"},
        body=b'''<div class="articleh" data-post-id="old-1001" data-publish-time="2026-08-11T13:59:00+08:00">
          <a class="l3" href="/news,bk0910,old-1001.html">old post</a>
          <span class="last_activity">2026-08-11T14:10:00+08:00</span>
        </div>''',
    )

    requests = list(spider.parse_job(response, job))

    assert [(request.url, request.cb_kwargs["job"]["kind"]) for request in requests] == [
        ("https://guba.eastmoney.com/list,bk0910_2.html", "list"),
        ("https://guba.eastmoney.com/comments/old-1001", "comments"),
    ]
    assert not (tmp_path / "records.jsonl").exists()


def test_invalid_list_activity_timestamp_stops_target_as_schema_changed(tmp_path: Path):
    """Catches malformed activity values causing unbounded pagination until quota exhaustion."""
    manifest = _manifest(
        tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html",
        settings_override={"circuit_breaker_failures": 1},
    )
    manifest.pop("test_hosts")
    job = manifest["jobs"][0]
    job["list_next_url_template"] = "https://guba.eastmoney.com/list,bk0910_{page}.html"
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    response = HtmlResponse(
        url=job["url"], encoding="utf-8", headers={b"Content-Type": b"application/json"},
        body=json.dumps({"re": [{
            "post_id": "bad-time", "post_publish_time": "not-a-timestamp",
            "post_last_time": "also-not-a-timestamp", "post_type": 0,
        }]}).encode(),
    )

    with pytest.raises(CloseSpider):
        list(spider.parse_job(response, job))
    assert spider.terminal_reason == "schema_changed"
    assert spider.failed_targets == {"bk0910"}


def test_list_row_without_published_or_activity_timestamp_stops_target(tmp_path: Path):
    """Catches blank public list rows causing pagination to continue without a time boundary."""
    manifest = _manifest(
        tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html",
        settings_override={"circuit_breaker_failures": 1},
    )
    manifest.pop("test_hosts")
    job = manifest["jobs"][0]
    job["list_next_url_template"] = "https://guba.eastmoney.com/list,bk0910_{page}.html"
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    response = HtmlResponse(
        url=job["url"], encoding="utf-8", headers={b"Content-Type": b"text/html"},
        body=b'<div class="articleh" data-post-id="no-time"><a class="l3">post</a></div>',
    )

    with pytest.raises(CloseSpider):
        list(spider.parse_job(response, job))
    assert spider.terminal_reason == "schema_changed"
    assert spider.failed_targets == {"bk0910"}


def test_old_active_post_does_not_consume_the_in_window_body_cap(tmp_path: Path):
    posts = [
        {
            "post_id": "old-1001", "post_title": "旧帖子", "post_content": "过期正文",
            "post_publish_time": "2026-08-11T13:59:00+08:00",
            "post_last_time": "2026-08-11T14:10:00+08:00",
            "post_comment_count": 1, "user_id": "old-author", "post_type": 0,
        },
        {
            "post_id": "new-1002", "post_title": "新帖子", "post_content": "窗口内正文",
            "post_publish_time": "2026-08-11T14:05:00+08:00",
            "post_last_time": "2026-08-11T14:05:00+08:00",
            "post_comment_count": 0, "user_id": "new-author", "post_type": 0,
        },
    ]
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html")
    manifest.pop("test_hosts")
    job = manifest["jobs"][0]
    job.update({"window_start": "2026-08-11T14:00:00+08:00", "posts_limit": 1})
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    response = HtmlResponse(
        url=job["url"], body=json.dumps({"re": posts}).encode(), encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    )

    requests = list(spider.parse_job(response, job))

    assert {(request.url, request.cb_kwargs["job"]["kind"]) for request in requests} == {
        ("https://guba.eastmoney.com/comments/old-1001", "comments"),
        ("https://guba.eastmoney.com/detail/new-1002", "detail"),
        ("https://guba.eastmoney.com/comments/new-1002", "comments"),
    }
    assert spider.seen_posts["bk0910"] == 1


def test_old_active_post_schedules_comments_when_reply_count_is_stale_or_zero(tmp_path: Path):
    old_post = {
        "post_id": "old-1001", "post_title": "旧帖子", "post_content": "过期正文",
        "post_publish_time": "2026-08-11T13:59:00+08:00",
        "post_last_time": "2026-08-11T14:10:00+08:00",
        "post_comment_count": 0, "user_id": "old-author", "post_type": 0,
    }
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html")
    manifest.pop("test_hosts")
    job = manifest["jobs"][0]
    job["window_start"] = "2026-08-11T14:00:00+08:00"
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    response = HtmlResponse(
        url=job["url"], body=json.dumps({"re": [old_post]}).encode(), encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    )

    requests = list(spider.parse_job(response, job))

    assert [(request.url, request.cb_kwargs["job"]["kind"]) for request in requests] == [
        ("https://guba.eastmoney.com/comments/old-1001", "comments"),
    ]


def test_duplicate_old_row_still_stops_list_pagination_at_window_floor(tmp_path: Path):
    old_post = {
        "post_id": "old-1001", "post_title": "旧帖子", "post_content": "过期正文",
        "post_publish_time": "2026-08-11T13:00:00+08:00",
        "post_last_time": "2026-08-11T13:00:00+08:00",
        "post_comment_count": 0, "user_id": "old-author", "post_type": 0,
    }
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk0910_2.html")
    manifest.pop("test_hosts")
    job = manifest["jobs"][0]
    job.update({
        "window_start": "2026-08-11T14:00:00+08:00",
        "list_next_url_template": "https://guba.eastmoney.com/list,bk0910_{page}.html",
        "page": 2,
        "seen_post_ids": ["old-1001"],
    })
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    response = HtmlResponse(
        url=job["url"], body=json.dumps({"re": [old_post]}).encode(), encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    )

    assert list(spider.parse_job(response, job)) == []


def test_duplicate_url_jobs_preserve_all_sector_mappings(tmp_path: Path, collector_server: str):
    manifest = _manifest(tmp_path, collector_server)
    duplicate = dict(manifest["jobs"][0], sector_id="compute", target_id="bk-compute")
    manifest["jobs"].append(duplicate)
    result, _ = _run_manifest(tmp_path, manifest)
    records = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]
    assert result.status == "ok"
    assert all(set(record["sector_ids"]) == {"robot", "compute"} for record in records)


def test_shared_url_failure_marks_every_mapping_and_retry_consumes_quota(
        tmp_path: Path, collector_server: str):
    manifest = _manifest(tmp_path, collector_server, path="/server-error", max_requests=2)
    manifest["settings"].update({"retry_times": 3, "retry_backoff_base_seconds": 0,
                                 "retry_backoff_max_seconds": 0})
    manifest["jobs"][0].pop("detail_url_template")
    manifest["jobs"][0].pop("comments_url_template")
    manifest["jobs"].append(dict(manifest["jobs"][0], sector_id="compute",
                                 target_id="bk-compute"))
    result, report = _run_manifest(tmp_path, manifest)
    payload = json.loads(report.read_text())
    assert result.status == "partial" and result.quota_exhausted is True
    assert payload["failed_targets"] == 2
    assert payload["request_count"] == 2
    assert _CollectorHandler.hits["/server-error"] == 2


def test_manifest_rejects_mixed_mapping_classification(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APEX_EASTMONEY_TESTING", "1")
    manifest = _manifest(tmp_path, "http://127.0.0.1")
    manifest["jobs"][0]["mappings"] = [
        {"target_id": "a", "sector_id": "robot", "source_type": "sector_forum",
         "stock_code": None, "pool_version": "v1"},
        {"target_id": "b", "sector_id": "robot", "source_type": "constituent_forum",
         "stock_code": "000001", "pool_version": "v1"},
    ]
    with pytest.raises(ValueError, match="classification"):
        _validate_manifest(manifest)


def test_manifest_rejects_separate_duplicate_jobs_with_mixed_classification(
        tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APEX_EASTMONEY_TESTING", "1")
    manifest = _manifest(tmp_path, "http://127.0.0.1")
    duplicate = dict(manifest["jobs"][0], target_id="stock-1", source_type="constituent_forum",
                     stock_code="000001")
    manifest["jobs"].append(duplicate)
    with pytest.raises(ValueError, match="classification"):
        _validate_manifest(manifest)


def test_completed_batch_rerun_is_idempotent(tmp_path: Path, collector_server: str):
    manifest = _manifest(tmp_path, collector_server)
    first, _ = _run_manifest(tmp_path, manifest)
    second, _ = _run_manifest(tmp_path, manifest)
    lines = (tmp_path / "records.jsonl").read_text().splitlines()
    assert first.status == second.status == "ok"
    assert len(lines) == 2
