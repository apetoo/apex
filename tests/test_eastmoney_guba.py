from __future__ import annotations

import json
import hashlib
import os
import threading
from types import SimpleNamespace
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from scrapy.exceptions import CloseSpider
from scrapy.http import FormRequest, HtmlResponse

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

# Reduced from the public page captured in the 2026-08-17 live batch.  The
# names/content are deliberately redacted; the `post_article` envelope and
# field names are the current page contract.
CURRENT_DETAIL_HTML = b'''<!doctype html><html><body>
<div class="newstitle">\xe6\x9c\xba\xe5\x99\xa8\xe4\xba\xba\xe6\x9d\xbf\xe5\x9d\x97</div>
<script>var post_article={
  "post_id":1759677380,
  "post_user":{"user_id":"public-author"},
  "post_guba":{"stockbar_code":"bk1106"},
  "post_title":"\xe5\xbd\x93\xe5\x89\x8d\xe9\xa1\xb5\xe9\x9d\xa2\xe5\xb8\x96\xe5\xad\x90",
  "post_content":"<p>\xe5\x88\x9b\xe6\x96\xb0\xe8\x8d\xaf\xe6\x9d\xbf\xe5\x9d\x97</p>",
  "post_publish_time":"2026-08-17 09:36:52",
  "post_last_time":"2026-08-17 09:39:51",
  "post_click_count":16,
  "post_comment_count":1,
  "post_like_count":0,
  "post_type":0
};</script></body></html>'''

# The public news page currently loads this through
# reply/api/Reply/ArticleNewReplyList.  It returns reply_* fields (and embeds
# nested replies), rather than the legacy comment_* shape.
CURRENT_FIRST_LEVEL_REPLY_RESPONSE = {
    "rc": 1,
    "re": [
        {
            "reply_id": "reply-in-window", "source_post_id": 1759677380,
            "reply_text": "窗口有反弹",
            "reply_publish_time": "2026-08-17 19:01:00",
            "reply_like_count": 2, "reply_is_like": False, "reply_is_top": False,
            "reply_is_author": False, "reply_state": 1, "reply_count": 1,
            "reply_picture": "", "user_id": "reply-author",
            "reply_user": {"user_id": "reply-author",
                                                "user_nickname": "公开用户"},
            "child_replys": [{"reply_id": "nested-reply"}], "fake_child_replys": [],
            "source_reply": None,
        },
        {
            "reply_id": "reply-before-window", "source_post_id": 1759677380,
            "reply_text": "旧评论",
            "reply_publish_time": "2026-08-16 17:59:00",
            "reply_like_count": 0, "reply_is_like": False, "reply_is_top": False,
            "reply_is_author": False, "reply_state": 1, "reply_count": 0,
            "reply_picture": "", "user_id": "old-author",
            "reply_user": {"user_id": "old-author",
                                                "user_nickname": "旧用户"},
            "child_replys": [], "fake_child_replys": [], "source_reply": None,
        },
    ],
    "fake_reply_list": [], "count": 2, "manager_comment_count": 0,
    "reply_total_count": 2, "ad_list": [], "ad_type_list": [],
    "post_comment_authority": 0, "top_reply_count": 0, "extend_data": {},
    "fake_switch": 0, "system_comment_authority": 0, "isExistHide": False,
    "user_jxreply_list": [], "user_jxreply_count": 0, "jxreply_wait_count": 0,
    "loc_reply": None, "latest_time": "2026-08-17 19:01:00",
    "me": "", "time": "2026-08-17T19:01:00+08:00", "loc_ext": None,
}


def current_reply_page(*, first_id: int, row_count: int, total_count: int,
                       published_at: str = "2026-08-17 19:01:00") -> dict:
    """Return a complete current API envelope with deterministic first-level rows."""
    payload = deepcopy(CURRENT_FIRST_LEVEL_REPLY_RESPONSE)
    rows = []
    for offset in range(row_count):
        row = deepcopy(CURRENT_FIRST_LEVEL_REPLY_RESPONSE["re"][0])
        row.update({
            "reply_id": f"reply-{first_id + offset}",
            "reply_text": f"公开评论 {first_id + offset}",
            "reply_publish_time": published_at,
            "reply_count": 0,
            "child_replys": [],
            "fake_child_replys": [],
        })
        rows.append(row)
    payload["re"] = rows
    payload["count"] = total_count
    payload["reply_total_count"] = total_count
    return payload


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


def test_parser_reads_current_article_list_html_into_exportable_public_post(tmp_path: Path):
    """Catches the live list page's embedded article_list payload being treated as a schema change."""
    # This is the relevant layout from the 2026-08-17 public bk1134 list response.
    live_layout = b'''<html><body><script>var article_list={"re":[{
      "post_id":1760184053,"post_title":"\xe8\xa6\x86\xe9\x93\x9c\xe6\x9d\xbf\xe6\xb6\xa8\xe4\xbb\xb7",
      "stockbar_code":"bk1134","user_id":"5676114794013740",
      "post_click_count":64,"post_comment_count":1,"post_like_count":0,
      "post_publish_time":"2026-08-17 19:13:59",
      "post_last_time":"2026-08-17 20:22:03","post_type":20
    }]};</script></body></html>'''

    posts = EastmoneyParser().parse_posts(live_layout, "text/html")

    assert posts == [{
        "content_id": "1760184053", "comment_id": "", "title": "覆铜板涨价", "text": "",
        "published_at": "2026-08-17 19:13:59", "author_id": "5676114794013740",
        "last_activity_at": "2026-08-17 20:22:03",
        "url": "https://guba.eastmoney.com/news,bk1134,1760184053.html",
        "read_count": 64, "reply_count": 1, "like_count": 0,
    }]
    output = tmp_path / "records.jsonl"
    assert EastmoneyExporter(output, author_salt="local-secret").export(
        posts, trade_date="2026-08-17", batch_id="live-layout", sector_id="compute",
        source_type="sector_forum", stock_code=None, pool_version="pool-v1",
        collected_at="2026-08-17T23:10:00+08:00",
    ) == 1
    assert json.loads(output.read_text())['url'] == (
        "https://guba.eastmoney.com/news,bk1134,1760184053.html"
    )


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


def test_current_detail_and_reply_api_export_canonical_url_and_only_in_window_first_level_comment(
    tmp_path: Path,
):
    """Catches the current `post_article`/`reply_*` contracts being marked schema-changed."""
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk1106.html")
    manifest.pop("test_hosts")
    job = manifest["jobs"][0]
    job.update({"forum_id": "bk1106", "window_start": "2026-08-16T18:00:00+08:00"})
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))

    detail_job = dict(job, kind="detail", content_id="1759677380",
                      url="https://guba.eastmoney.com/news,bk1106,1759677380.html")
    detail_response = HtmlResponse(
        url=detail_job["url"], body=CURRENT_DETAIL_HTML, encoding="utf-8",
        headers={b"Content-Type": b"text/html; charset=utf-8"},
    )
    assert list(spider.parse_job(detail_response, detail_job)) == []

    comments_job = dict(job, kind="comments", content_id="1759677380",
                        url="https://guba.eastmoney.com/api/getData")
    comments_response = HtmlResponse(
        url=comments_job["url"],
        body=json.dumps(CURRENT_FIRST_LEVEL_REPLY_RESPONSE).encode(), encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    )
    assert list(spider.parse_job(comments_response, comments_job)) == []
    assert spider.parser_errors == 0

    records = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]
    assert [(record["content_id"], record["comment_id"], record["url"])
            for record in records] == [
                ("1759677380", "", "https://guba.eastmoney.com/news,bk1106,1759677380.html"),
                ("1759677380", "reply-in-window",
                 "https://guba.eastmoney.com/news,bk1106,1759677380.html"),
            ]


def test_current_reply_api_contract_keeps_only_first_level_comments_inside_window():
    """Catches reply_* API rows being mistaken for the retired comment_* response."""
    page = EastmoneyParser().parse_comment_page(
        json.dumps(CURRENT_FIRST_LEVEL_REPLY_RESPONSE).encode(), "application/json",
        window_start="2026-08-16T18:00:00+08:00", content_id="1759677380",
    )

    assert [(row["content_id"], row["comment_id"], row["text"])
            for row in page.records] == [
                ("1759677380", "reply-in-window", "窗口有反弹"),
            ]
    assert page.window_exhausted is True


def test_current_reply_window_compares_real_timestamps_across_api_and_iso_formats():
    """Keeps same-day replies after an ISO lower bound despite Eastmoney's space separator."""
    payload = current_reply_page(
        first_id=1, row_count=1, total_count=1,
        published_at="2026-08-17 19:01:00",
    )

    page = EastmoneyParser().parse_comment_page(
        json.dumps(payload).encode(), "application/json",
        window_start="2026-08-17T18:00:00+08:00", content_id="1759677380",
    )

    assert [row["comment_id"] for row in page.records] == ["reply-1"]
    assert page.window_exhausted is False


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


def assert_current_public_comment_request(request, content_id: str) -> None:
    """Assert the observable public request, including the comment identity it carries."""
    assert request.cb_kwargs["job"]["kind"] == "comments"
    assert request.cb_kwargs["job"]["content_id"] == content_id
    assert request.method == "GET"
    parsed = urlparse(request.url)
    assert parsed.netloc == "gbapi.eastmoney.com"
    assert parsed.path == "/reply/JSONP/ArticleNewReplyList"
    assert parse_qs(parsed.query)["postid"] == [content_id]


def test_comment_parser_accepts_bounded_jsonp_reply_payload():
    """Catches the public JSONP transport being rejected before contract validation."""
    parser = EastmoneyParser()
    payload = current_reply_page(first_id=1, row_count=1, total_count=1)
    body = f"jQuery_apex({json.dumps(payload)});".encode()

    page = parser.parse_comment_page(
        body, "application/javascript", content_id="1759677380", page=1,
    )

    assert [record["comment_id"] for record in page.records] == ["reply-1"]
    assert page.has_more is False


def test_comment_parser_uses_json_body_when_backup_mislabels_content_type():
    """Catches the public backup returning valid reply JSON as text/html."""
    parser = EastmoneyParser()
    payload = current_reply_page(first_id=1, row_count=1, total_count=1)

    page = parser.parse_comment_page(
        json.dumps(payload).encode(), "text/html; charset=utf-8",
        content_id="1759677380", page=1,
    )

    assert [record["comment_id"] for record in page.records] == ["reply-1"]


def test_legacy_comment_template_uses_public_no_cookie_primary_reply_api(tmp_path: Path):
    """Catches comments reverting to the retired GET endpoint or requiring browser login state."""
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk1106.html")
    manifest.pop("test_hosts")
    job = manifest["jobs"][0]
    # This is the old, allowlisted config value that existing installations
    # have.  The collector must upgrade it at the request boundary instead of
    # requiring users to edit a secret/session-dependent endpoint.
    comment_job = dict(job, kind="comments", content_id="1759677380",
                       url="https://guba.eastmoney.com/comments/1759677380",
                       forum_id="bk1106")
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))

    request = spider._request(comment_job)

    assert request.method == "GET"
    parsed = urlparse(request.url)
    assert parsed.netloc == "gbapi.eastmoney.com"
    assert parsed.path == "/reply/JSONP/ArticleNewReplyList"
    assert parse_qs(parsed.query) == {
        "callback": ["jQuery_apex"], "plat": ["web"], "version": ["300"],
        "product": ["guba"], "postid": ["1759677380"], "sort": ["1"],
        "sorttype": ["1"], "p": ["1"], "ps": ["30"], "type": ["0"],
        "h": [hashlib.md5(b"1759677380").hexdigest()],
    }
    assert request.headers.getlist(b"Referer") == [
        b"https://guba.eastmoney.com/news,bk1106,1759677380.html",
    ]
    assert b"Cookie" not in request.headers


def test_primary_comment_schema_failure_retries_same_post_on_backup_once(tmp_path: Path):
    """Catches a false-success primary payload dropping comments or retrying per target."""
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html")
    manifest.pop("test_hosts")
    job = dict(manifest["jobs"][0], kind="comments", content_id="1001",
               forum_id="bk0910", url="https://guba.eastmoney.com/comments/1001")
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    request = spider._request(job)
    response = HtmlResponse(
        url=request.url,
        body=json.dumps({"re": True, "result": [{"security": "1$600111$1"}]}).encode(),
        encoding="utf-8", headers={b"Content-Type": b"application/json"},
    )

    children = list(spider.parse_job(response, request.cb_kwargs["job"]))

    assert len(children) == 1
    backup = children[0]
    assert isinstance(backup, FormRequest)
    assert backup.url == "https://guba.eastmoney.com/interface/GetData.aspx"
    assert backup.cb_kwargs["job"]["comment_source"] == "backup"
    assert spider.comment_fallback_used is True
    assert spider.comment_source_failures == {"primary": 1, "backup": 0}


def test_both_comment_sources_fail_once_then_open_comment_only_circuit(tmp_path: Path):
    """Catches repeated malformed comments consuming the batch or closing the post spider."""
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html")
    manifest.pop("test_hosts")
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    malformed = json.dumps({"re": True, "result": []}).encode()
    job = dict(manifest["jobs"][0], kind="comments", content_id="1001",
               forum_id="bk0910", url="https://guba.eastmoney.com/comments/1001")
    primary = spider._request(job)
    fallback = list(spider.parse_job(HtmlResponse(
        url=primary.url, body=malformed, encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    ), primary.cb_kwargs["job"]))[0]

    assert list(spider.parse_job(HtmlResponse(
        url=fallback.url, body=malformed, encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    ), fallback.cb_kwargs["job"])) == []
    assert spider.comment_circuit_open is True
    assert spider.comment_source_failures == {"primary": 1, "backup": 1}
    assert spider._request(dict(job, content_id="1002")) is None
    assert spider.terminal_reason is None
    spider.records = 1
    spider.closed("finished")
    report = json.loads((tmp_path / "report.json").read_text())
    assert EastmoneyRunner.classify_result(report).status == "partial"


def test_primary_comment_network_failure_switches_to_backup(tmp_path: Path):
    """Catches transport failures bypassing the same fallback used for bad contracts."""
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html")
    manifest.pop("test_hosts")
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    primary = spider._request(dict(
        manifest["jobs"][0], kind="comments", content_id="1001", forum_id="bk0910",
    ))

    backup = spider.request_failed(SimpleNamespace(request=primary))

    assert isinstance(backup, FormRequest)
    assert backup.url == "https://guba.eastmoney.com/interface/GetData.aspx"
    assert spider.comment_fallback_used is True


def test_primary_comment_access_control_switches_to_backup_without_global_close(tmp_path: Path):
    """Catches a blocked comment endpoint closing otherwise healthy post collection."""
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html")
    manifest.pop("test_hosts")
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    primary = spider._request(dict(
        manifest["jobs"][0], kind="comments", content_id="1001", forum_id="bk0910",
    ))
    response = HtmlResponse(
        url=primary.url, body="<html>请输入验证码</html>".encode(), encoding="utf-8",
        headers={b"Content-Type": b"text/html"},
    )

    children = list(spider.parse_job(response, primary.cb_kwargs["job"]))

    assert len(children) == 1
    assert children[0].cb_kwargs["job"]["comment_source"] == "backup"
    assert spider.terminal_reason is None


def test_comment_subquota_stops_comments_without_consuming_core_quota(tmp_path: Path):
    """Catches comments exhausting the shared request budget before detail requests run."""
    manifest = _manifest(
        tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html",
        settings_override={"max_requests": 10, "max_comment_requests": 1},
    )
    manifest.pop("test_hosts")
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    base = manifest["jobs"][0]

    assert spider._request(dict(base, kind="comments", content_id="1001")) is not None
    assert spider._request(dict(base, kind="comments", content_id="1002")) is None
    assert spider._request(dict(base, kind="detail", content_id="1002")) is not None
    assert spider.quota.request_count == 2
    assert spider.comment_quota.exhausted is True


def test_current_reply_api_count_uses_thirty_row_pages_and_stops_at_window_floor():
    """Catches pagination based on retired has_more/next_cursor fields instead of API count."""
    parser = EastmoneyParser()
    first = parser.parse_comment_page(
        json.dumps(current_reply_page(first_id=1, row_count=30, total_count=50)).encode(),
        "application/json", content_id="1759677380", page=1,
        window_start="2026-08-16T18:00:00+08:00",
    )
    second = parser.parse_comment_page(
        json.dumps(current_reply_page(first_id=31, row_count=20, total_count=50)).encode(),
        "application/json", content_id="1759677380", page=2,
        window_start="2026-08-16T18:00:00+08:00",
    )
    exhausted = parser.parse_comment_page(
        json.dumps(current_reply_page(
            first_id=1, row_count=30, total_count=50,
            published_at="2026-08-16T17:59:00+08:00",
        )).encode(),
        "application/json", content_id="1759677380", page=1,
        window_start="2026-08-16T18:00:00+08:00",
    )

    assert len(first.records) == 30
    assert first.has_more is True and first.next_cursor == "2"
    assert len(second.records) == 20
    assert second.has_more is False and second.next_cursor is None
    assert exhausted.records == []
    assert exhausted.window_exhausted is True
    assert exhausted.has_more is False and exhausted.next_cursor is None


def test_current_reply_api_spider_requests_page_two_and_enforces_fifty_comment_cap(tmp_path: Path):
    """Catches current reply paging that either skips p=2 or exports beyond comments_per_post."""
    manifest = _manifest(tmp_path, "https://guba.eastmoney.com", path="/list,bk1106.html")
    manifest.pop("test_hosts")
    manifest["settings"]["comments_per_post"] = 50
    job = dict(manifest["jobs"][0], kind="comments", content_id="1759677380",
               forum_id="bk1106", url="https://guba.eastmoney.com/comments/1759677380",
               comments_next_url_template="https://guba.eastmoney.com/comments/{content_id}?p={cursor}",
               window_start="2026-08-16T18:00:00+08:00")
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))

    first_response = HtmlResponse(
        url=job["url"], body=json.dumps(
            current_reply_page(first_id=1, row_count=30, total_count=80),
        ).encode(), encoding="utf-8", headers={b"Content-Type": b"application/json"},
    )
    children = list(spider.parse_job(first_response, job))
    assert len(children) == 1
    page_two = children[0]
    assert page_two.method == "GET"
    assert parse_qs(urlparse(page_two.url).query)["p"] == ["2"]

    second_response = HtmlResponse(
        url=page_two.url, body=json.dumps(
            current_reply_page(first_id=31, row_count=30, total_count=80),
        ).encode(), encoding="utf-8", headers={b"Content-Type": b"application/json"},
    )
    assert list(spider.parse_job(second_response, page_two.cb_kwargs["job"])) == []
    records = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]
    assert len(records) == 50
    assert [record["comment_id"] for record in records] == [
        *(f"reply-{index}" for index in range(1, 51)),
    ]


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


def test_unrelated_comment_security_payload_degrades_only_comments_and_preserves_posts(
    tmp_path: Path,
):
    """Catches a no-cookie reply response tripping the global list/detail schema circuit."""
    manifest = _manifest(
        tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html",
        settings_override={"circuit_breaker_failures": 1},
    )
    manifest.pop("test_hosts")
    job = manifest["jobs"][0]
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    detail_job = dict(job, kind="detail", content_id="1001",
                      url="https://guba.eastmoney.com/news,bk0910,1001.html")
    detail_response = HtmlResponse(
        url=detail_job["url"], body=fixture("posts.json"), encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    )
    assert list(spider.parse_job(detail_response, detail_job)) == []

    # This is the observed unauthenticated false-success body from the comment
    # proxy: it is JSON, but not an ArticleNewReplyList payload.
    security_payload = {"re": True, "result": [{"security": "1$600111$12050879181666"}]}
    comment_job = dict(job, kind="comments", content_id="1001",
                       url="https://guba.eastmoney.com/comments/1001")
    primary = spider._request(comment_job)
    backup = list(spider.parse_job(HtmlResponse(
        url=primary.url, body=json.dumps(security_payload).encode(), encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    ), primary.cb_kwargs["job"]))[0]
    assert list(spider.parse_job(HtmlResponse(
        url=backup.url, body=json.dumps(security_payload).encode(), encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    ), backup.cb_kwargs["job"])) == []

    spider.closed("finished")
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["records"] == 1
    assert report["parser_errors"] == 2
    assert report["failed_targets"] == 0
    assert "terminal_reason" not in report
    assert EastmoneyRunner.classify_result(report).status == "partial"


def test_non_object_comment_row_degrades_with_audited_parser_error_and_preserves_posts(
    tmp_path: Path,
):
    """Treats malformed rows as comment schema drift rather than an unhandled spider error."""
    manifest = _manifest(
        tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html",
        settings_override={"circuit_breaker_failures": 1},
    )
    manifest.pop("test_hosts")
    job = manifest["jobs"][0]
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    detail_job = dict(job, kind="detail", content_id="1001",
                      url="https://guba.eastmoney.com/news,bk0910,1001.html")
    detail_response = HtmlResponse(
        url=detail_job["url"], body=fixture("posts.json"), encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    )
    assert list(spider.parse_job(detail_response, detail_job)) == []

    comment_job = dict(job, kind="comments", content_id="1001",
                       url="https://guba.eastmoney.com/comments/1001")
    malformed_response = HtmlResponse(
        url=comment_job["url"], body=json.dumps({"re": [True], "count": 1}).encode(),
        encoding="utf-8", headers={b"Content-Type": b"application/json"},
    )

    primary = spider._request(comment_job)
    fallback = list(spider.parse_job(malformed_response, primary.cb_kwargs["job"]))[0]
    assert list(spider.parse_job(malformed_response, fallback.cb_kwargs["job"])) == []
    spider.closed("finished")
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["records"] == 1
    assert report["parser_errors"] == 2
    assert report["failed_targets"] == 0
    assert "terminal_reason" not in report
    assert EastmoneyRunner.classify_result(report).status == "partial"


def test_detail_schema_change_still_opens_the_global_circuit(tmp_path: Path):
    """Catches a policy change that would silently continue after malformed post evidence."""
    manifest = _manifest(
        tmp_path, "https://guba.eastmoney.com", path="/list,bk0910.html",
        settings_override={"circuit_breaker_failures": 1},
    )
    manifest.pop("test_hosts")
    job = dict(manifest["jobs"][0], kind="detail", content_id="1001",
               url="https://guba.eastmoney.com/news,bk0910,1001.html")
    spider = EastmoneySpider(manifest=manifest, report_path=str(tmp_path / "report.json"))
    response = HtmlResponse(
        url=job["url"], body=b'{"re": true}', encoding="utf-8",
        headers={b"Content-Type": b"application/json"},
    )

    with pytest.raises(CloseSpider):
        list(spider.parse_job(response, job))
    assert spider.terminal_reason == "schema_changed"


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

    assert requests[0].url == "https://guba.eastmoney.com/list,bk0910_2.html"
    assert requests[0].cb_kwargs["job"]["kind"] == "list"
    assert_current_public_comment_request(requests[1], "old-1001")
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

    assert requests[0].url == "https://guba.eastmoney.com/list,bk0910_2.html"
    assert requests[0].cb_kwargs["job"]["kind"] == "list"
    assert_current_public_comment_request(requests[1], "old-1001")
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

    assert [(request.cb_kwargs["job"]["kind"], request.cb_kwargs["job"].get("content_id"))
            for request in requests] == [
                ("comments", "old-1001"),
                ("detail", None),
                ("comments", "new-1002"),
            ]
    assert_current_public_comment_request(requests[0], "old-1001")
    assert requests[1].url == "https://guba.eastmoney.com/detail/new-1002"
    assert_current_public_comment_request(requests[2], "new-1002")
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

    assert len(requests) == 1
    assert_current_public_comment_request(requests[0], "old-1001")


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
