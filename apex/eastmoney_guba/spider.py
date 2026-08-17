"""Scrapy process boundary for public, unauthenticated Eastmoney Guba jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import math
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import CloseSpider
from scrapy.downloadermiddlewares.retry import RetryMiddleware
from twisted.internet.task import deferLater

from .exporter import EastmoneyExporter
from .models import QuotaBudget
from .parser import BlockedResponse, EastmoneyParser, SchemaChanged


_KINDS = {"list", "detail", "comments"}
_PRODUCTION_HOSTS = {"guba.eastmoney.com", "gbapi.eastmoney.com"}
_BUDGETS = ("max_requests", "requests_per_target", "posts_per_target", "comments_per_post",
            "retry_times", "timeout_seconds", "concurrent_requests_per_domain",
            "circuit_breaker_failures")


def _strict_nonnegative(settings: dict, key: str, default: int, *, positive: bool = False) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{key} must be a {qualifier} integer")
    return value


def _allowed_url(url: str, test_hosts: set[str]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname in _PRODUCTION_HOSTS:
        return True
    return (os.environ.get("APEX_EASTMONEY_TESTING") == "1"
            and parsed.scheme in {"http", "https"} and parsed.hostname in test_hosts)


def _validate_manifest(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
        raise ValueError("manifest requires a jobs list")
    required = ("trade_date", "batch_id", "collected_at", "output_file", "author_salt")
    if any(not value.get(key) for key in required):
        raise ValueError("manifest missing required batch/export fields")
    settings = value.get("settings") or {}
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    for key in _BUDGETS:
        _strict_nonnegative(settings, key, {"max_requests": 1000, "requests_per_target": 100,
                                            "posts_per_target": 100, "comments_per_post": 50,
                                            "retry_times": 3, "timeout_seconds": 20,
                                            "concurrent_requests_per_domain": 1,
                                            "circuit_breaker_failures": 5}[key],
                            positive=key in {"timeout_seconds", "concurrent_requests_per_domain",
                                             "circuit_breaker_failures"})
    if _strict_nonnegative(settings, "comments_per_post", 50) > 50:
        raise ValueError("comments_per_post must not exceed 50")
    injected = value.get("test_hosts") or []
    if not isinstance(injected, list) or any(host not in {"127.0.0.1", "localhost", "::1"}
                                             for host in injected):
        raise ValueError("test_hosts may contain loopback hosts only")
    if injected and os.environ.get("APEX_EASTMONEY_TESTING") != "1":
        raise ValueError("test_hosts require APEX_EASTMONEY_TESTING=1")
    test_hosts = set(injected)
    merged: dict[tuple, dict] = {}
    for job in value["jobs"]:
        if not isinstance(job, dict) or job.get("kind") not in _KINDS or not job.get("url"):
            raise ValueError("each job requires a typed kind and URL")
        for key in ("target_id", "sector_id", "source_type", "pool_version"):
            if not job.get(key):
                raise ValueError(f"job missing {key}")
        urls = [job["url"], job.get("list_next_url_template"),
                job.get("detail_url_template"), job.get("comments_url_template"),
                job.get("comments_next_url_template")]
        if any(url and not _allowed_url(url.replace("{content_id}", "1")
                                        .replace("{cursor}", "1").replace("{page}", "2"),
                                        test_hosts) for url in urls):
            raise ValueError("collector URLs must use HTTPS Eastmoney allowlist")
        key = (job["kind"], job["url"], job.get("list_next_url_template"),
               job.get("detail_url_template"),
               job.get("comments_url_template"), job.get("comments_next_url_template"))
        mapping = {name: job.get(name) for name in
                   ("target_id", "sector_id", "source_type", "stock_code", "pool_version")}
        mappings = job.get("mappings") or [mapping]
        if (not isinstance(mappings, list) or any(not isinstance(item, dict)
                                                  for item in mappings)):
            raise ValueError("job mappings must be a list of objects")
        if any(any(not item.get(name) for name in
                   ("target_id", "sector_id", "source_type", "pool_version")) for item in mappings):
            raise ValueError("every job mapping requires target, sector, source type, and version")
        classifications = {(item["source_type"], item.get("stock_code"), item["pool_version"])
                           for item in mappings}
        if len(classifications) != 1:
            raise ValueError("merged mappings must share one source classification")
        if key not in merged:
            merged[key] = dict(job, mappings=list(mappings))
        else:
            merged[key]["mappings"].extend(mappings)
    value = dict(value)
    value["jobs"] = list(merged.values())
    for job in value["jobs"]:
        classifications = {(item["source_type"], item.get("stock_code"), item["pool_version"])
                           for item in job["mappings"]}
        if len(classifications) != 1:
            raise ValueError("merged mappings must share one source classification")
    return value


class ExponentialRetryMiddleware(RetryMiddleware):
    """Bounded RetryMiddleware with a real non-blocking exponential delay."""

    def __init__(self, settings):
        super().__init__(settings)
        self.backoff_base = settings.getfloat("RETRY_BACKOFF_BASE_SECONDS", 1.0)
        self.backoff_cap = settings.getfloat("RETRY_BACKOFF_MAX_SECONDS", 30.0)

    @staticmethod
    def delay_for(attempt: int, *, base: float, cap: float) -> float:
        return min(cap, base * (2 ** max(0, attempt - 1)))

    def _retry(self, request, reason, spider):
        retry = super()._retry(request, reason, spider)
        if retry is None:
            return None
        target_id = str(request.meta.get("quota_target_id") or "retry")
        if not spider.quota.consume(target_id):
            return None
        delay = self.delay_for(int(retry.meta.get("retry_times", 1)),
                               base=self.backoff_base, cap=self.backoff_cap)
        from twisted.internet import reactor
        return deferLater(reactor, delay, lambda: retry)


class EastmoneySpider(scrapy.Spider):
    name = "eastmoney_guba"
    allowed_domains = sorted(_PRODUCTION_HOSTS)
    handle_httpstatus_list = [403]
    custom_settings = {
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "AUTOTHROTTLE_ENABLED": True,
        "COOKIES_ENABLED": False,
        "LOG_LEVEL": "WARNING",
        "LOGSTATS_INTERVAL": 0,
        "RETRY_HTTP_CODES": [429, 500, 502, 503, 504],
        "RETRY_PRIORITY_ADJUST": -1,
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy.downloadermiddlewares.retry.RetryMiddleware": None,
            "apex.eastmoney_guba.spider.ExponentialRetryMiddleware": 550,
        },
        "USER_AGENT": "ApexLocalResearch/1.0 (public, low-frequency research collector)",
    }

    def __init__(self, manifest: dict, report_path: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.manifest = _validate_manifest(manifest)
        if os.environ.get("APEX_EASTMONEY_TESTING") == "1":
            self.allowed_domains = sorted(_PRODUCTION_HOSTS | set(manifest.get("test_hosts") or []))
        self.report_path = Path(report_path)
        settings = manifest.get("settings") or {}
        self.quota = QuotaBudget(_strict_nonnegative(settings, "max_requests", 1000),
                                 _strict_nonnegative(settings, "requests_per_target", 100))
        self.posts_per_target = _strict_nonnegative(settings, "posts_per_target", 100)
        self.comments_per_post = _strict_nonnegative(settings, "comments_per_post", 50)
        self.parser = EastmoneyParser()
        self.exporter = EastmoneyExporter(Path(manifest["output_file"]),
                                          author_salt=str(manifest["author_salt"]))
        self.records = 0
        self.failed_targets: set[str] = set()
        self.terminal_reason: str | None = None
        self.seen_posts: dict[str, int] = {}
        self.parser_successes = 0
        self.parser_errors = 0
        self.request_successes = 0
        self.request_errors = 0
        self.circuit_breaker_failures = _strict_nonnegative(
            settings, "circuit_breaker_failures", 5, positive=True,
        )

    def start_requests(self):
        for job in self.manifest["jobs"]:
            request = self._request(job)
            if request is not None:
                yield request

    def _request(self, job: dict):
        target_id = ",".join(sorted(str(mapping["target_id"])
                                    for mapping in job.get("mappings", [job])))
        if not self.quota.consume(target_id):
            return None
        return scrapy.Request(job["url"], callback=self.parse_job, errback=self.request_failed,
                              cb_kwargs={"job": job}, meta={"quota_target_id": target_id},
                              # Explicit manifests are already URL-premerged. Reissuing requests is
                              # required for safe restart when JOBDIR's dupefilter contains a request
                              # that completed just before an interrupted process.
                              dont_filter=True)

    def parse_job(self, response, job: dict):
        self._snapshot_response(response, job)
        if response.status == 403:
            self.request_errors += 1
            self.terminal_reason = "blocked"
            raise CloseSpider("blocked")
        self.request_successes += 1
        content_type = response.headers.get(b"Content-Type", b"").decode("latin1")
        try:
            if job["kind"] == "comments":
                page = self.parser.parse_comment_page(response.body, content_type,
                                                      window_start=job.get("window_start"))
                self.parser_successes += 1
                seen = set(job.get("seen_comment_ids") or [])
                unique = [record for record in page.records
                          if record["comment_id"] not in seen]
                records = unique[: max(0, self.comments_per_post - len(seen))]
                records = [self._with_source_url(record, job) for record in records]
                self._export(records, job)
                seen.update(record["comment_id"] for record in records)
                if (page.has_more and page.next_cursor and len(seen) < self.comments_per_post
                        and job.get("comments_next_url_template")):
                    child = dict(job, url=job["comments_next_url_template"].format(
                        content_id=job.get("content_id", ""), cursor=page.next_cursor),
                                 cursor=page.next_cursor, seen_comment_ids=sorted(seen))
                    request = self._request(child)
                    if request is not None:
                        yield request
                return
            posts = self.parser.parse_posts(response.body, content_type)
        except BlockedResponse:
            self.terminal_reason = "blocked"
            raise CloseSpider("blocked")
        except SchemaChanged:
            self._schema_changed(job)
            return
        self.parser_successes += 1

        if job["kind"] == "detail":
            self._export([self._with_source_url(post, job) for post in posts[:1]], job)
            return

        window_start = job.get("window_start")
        lower = None
        if window_start:
            try:
                lower = datetime.fromisoformat(str(window_start).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                raise CloseSpider("invalid_window_timestamp")
        posts_limit = job.get("posts_limit", self.posts_per_target)
        if isinstance(posts_limit, bool) or not isinstance(posts_limit, int) or posts_limit < 0:
            raise CloseSpider("invalid_posts_limit")
        cap = min(self.posts_per_target, posts_limit)
        seen_ids = set(job.get("seen_post_ids") or [])
        scheduled_count = int(job.get("scheduled_post_count") or 0)
        children = []
        page_activities: list[datetime] = []
        parsed_posts = []
        for post in posts:
            try:
                published = self._timestamp(post.get("published_at"), lower)
                activity = self._timestamp(
                    post.get("last_activity_at") or post.get("published_at"), lower,
                )
            except (TypeError, ValueError):
                self._schema_changed(job)
                return
            if published is None and activity is None:
                self._schema_changed(job)
                return
            if activity is not None:
                page_activities.append(activity)
            parsed_posts.append((post, published, activity))
        for post, published, activity in parsed_posts:
            content_id = post["content_id"]
            if content_id in seen_ids:
                continue
            body_in_window = lower is None or (published is not None and published >= lower)
            active_in_window = lower is None or (activity is not None and activity >= lower)
            if not body_in_window and not active_in_window:
                continue
            seen_ids.add(content_id)
            if body_in_window and scheduled_count < cap:
                scheduled_count += 1
                detail_template = job.get("detail_url_template")
                if detail_template:
                    child = dict(job, kind="detail", url=detail_template.format(content_id=content_id))
                    request = self._request(child)
                    if request is not None:
                        children.append(request)
                else:
                    self._export([self._with_source_url(post, job)], job)
            elif body_in_window:
                continue
            comments_template = job.get("comments_url_template")
            if comments_template and (body_in_window or active_in_window):
                child = dict(job, kind="comments", content_id=content_id,
                             url=comments_template.format(content_id=content_id))
                request = self._request(child)
                if request is not None:
                    children.append(request)
        self.seen_posts[job["target_id"]] = scheduled_count

        page = job.get("page", 1)
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise CloseSpider("invalid_list_page")
        crossed_floor = bool(lower is not None and page_activities
                             and page_activities[-1] < lower)
        next_template = job.get("list_next_url_template")
        if next_template and scheduled_count < cap and posts and not crossed_floor:
            child = dict(
                job, kind="list", page=page + 1,
                url=next_template.format(page=page + 1),
                seen_post_ids=sorted(seen_ids), scheduled_post_count=scheduled_count,
            )
            request = self._request(child)
            if request is not None:
                yield request
        yield from children

    @staticmethod
    def _timestamp(value, lower: datetime | None) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if lower is not None and parsed.tzinfo is None and lower.tzinfo is not None:
            parsed = parsed.replace(tzinfo=lower.tzinfo)
        return parsed

    def _schema_changed(self, job: dict) -> None:
        self.parser_errors += 1
        for mapping in job.get("mappings") or [job]:
            self.failed_targets.add(str(mapping.get("target_id") or "unknown"))
        if self.parser_errors >= self.circuit_breaker_failures:
            self.terminal_reason = "schema_changed"
            raise CloseSpider("schema_changed")

    @staticmethod
    def _canonical_detail_url(job: dict, content_id: str) -> str:
        forum_id = str(job.get("forum_id") or job.get("target_id") or "").strip()
        return f"https://guba.eastmoney.com/news,{forum_id},{content_id}.html"

    def _with_source_url(self, record: dict, job: dict) -> dict:
        value = dict(record)
        value["url"] = self._canonical_detail_url(job, str(value["content_id"]))
        return value

    def _export(self, records: list[dict], job: dict):
        mappings = job.get("mappings") or [job]
        self.exporter.export(
            records, trade_date=self.manifest["trade_date"], batch_id=self.manifest["batch_id"],
            sector_id=mappings[0]["sector_id"], source_type=mappings[0]["source_type"],
            stock_code=mappings[0].get("stock_code"), pool_version=mappings[0]["pool_version"],
            collected_at=self.manifest["collected_at"],
            sector_ids=[mapping["sector_id"] for mapping in mappings],
            target_mappings=mappings,
        )
        self.records += len(records)

    def _snapshot_response(self, response, job: dict) -> None:
        raw_dir = self.manifest.get("raw_response_dir")
        if not raw_dir:
            return
        identity = hashlib.sha256(
            (str(job.get("kind")) + "\0" + response.url + "\0").encode("utf-8")
            + response.body
        ).hexdigest()
        path = Path(raw_dir) / f"{identity}.body"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(response.body)
        except FileExistsError:
            pass

    def request_failed(self, failure):
        self.request_errors += 1
        request = failure.request
        job = request.cb_kwargs.get("job", {})
        for mapping in job.get("mappings") or [job]:
            self.failed_targets.add(str(mapping.get("target_id") or "unknown"))

    def closed(self, reason):
        report = {
            "records": self.records, "request_count": self.quota.request_count,
            "failed_targets": len(self.failed_targets),
            "quota_exhausted": self.quota.exhausted,
            "parser_successes": self.parser_successes,
            "parser_errors": self.parser_errors,
            "request_successes": self.request_successes,
            "request_errors": self.request_errors,
        }
        if self.terminal_reason:
            report["terminal_reason"] = self.terminal_reason
        elif reason not in {"finished", "shutdown"}:
            report["terminal_reason"] = "failed"
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        temporary.replace(self.report_path)
        if reason == "finished" and self.manifest.get("jobdir"):
            Path(self.manifest["jobdir"]).mkdir(parents=True, exist_ok=True)
            Path(self.manifest["jobdir"], ".completed").write_text("ok", encoding="utf-8")


def main() -> int:
    cli = argparse.ArgumentParser()
    cli.add_argument("--jobs", required=True)
    cli.add_argument("--report", required=True)
    args = cli.parse_args()
    manifest = _validate_manifest(json.loads(Path(args.jobs).read_text(encoding="utf-8")))
    configured = manifest.get("settings") or {}
    for key, default in (("retry_backoff_base_seconds", 1.0),
                         ("retry_backoff_max_seconds", 30.0),
                         ("download_delay_seconds", 2.0)):
        value = configured.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be a finite non-negative number")
    jobdir = Path(manifest["jobdir"]) if manifest.get("jobdir") else None
    if jobdir and (jobdir / ".completed").exists():
        shutil.rmtree(jobdir)
    process_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": _strict_nonnegative(
            configured, "concurrent_requests_per_domain", 1, positive=True),
        "DOWNLOAD_DELAY": float(configured.get("download_delay_seconds", 2)),
        "DOWNLOAD_TIMEOUT": int(configured.get("timeout_seconds", 20)),
        "RETRY_TIMES": _strict_nonnegative(configured, "retry_times", 3),
        "RETRY_BACKOFF_BASE_SECONDS": float(configured.get("retry_backoff_base_seconds", 1)),
        "RETRY_BACKOFF_MAX_SECONDS": float(configured.get("retry_backoff_max_seconds", 30)),
        "JOBDIR": manifest.get("jobdir"),
        "TELNETCONSOLE_ENABLED": False,
        "ROBOTSTXT_OBEY": False,
    }
    process = CrawlerProcess(settings=process_settings)
    process.crawl(EastmoneySpider, manifest=manifest, report_path=args.report)
    process.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
