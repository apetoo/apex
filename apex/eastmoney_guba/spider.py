"""Scrapy process boundary for public, unauthenticated Eastmoney Guba jobs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import math
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
            "retry_times", "timeout_seconds", "concurrent_requests_per_domain")


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
                                            "concurrent_requests_per_domain": 1}[key],
                            positive=key in {"timeout_seconds", "concurrent_requests_per_domain"})
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
        urls = [job["url"], job.get("detail_url_template"), job.get("comments_url_template"),
                job.get("comments_next_url_template")]
        if any(url and not _allowed_url(url.replace("{content_id}", "1").replace("{cursor}", "1"),
                                        test_hosts) for url in urls):
            raise ValueError("collector URLs must use HTTPS Eastmoney allowlist")
        key = (job["kind"], job["url"], job.get("detail_url_template"),
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
        if key not in merged:
            merged[key] = dict(job, mappings=list(mappings))
        else:
            merged[key]["mappings"].extend(mappings)
    value = dict(value)
    value["jobs"] = list(merged.values())
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
        if response.status == 403:
            self.terminal_reason = "blocked"
            raise CloseSpider("blocked")
        content_type = response.headers.get(b"Content-Type", b"").decode("latin1")
        try:
            if job["kind"] == "comments":
                page = self.parser.parse_comment_page(response.body, content_type,
                                                      window_start=job.get("window_start"))
                used = int(job.get("comments_collected", 0))
                records = page.records[: max(0, self.comments_per_post - used)]
                self._export(records, job)
                used += len(records)
                if (page.has_more and page.next_cursor and used < self.comments_per_post
                        and job.get("comments_next_url_template")):
                    child = dict(job, url=job["comments_next_url_template"].format(
                        content_id=job.get("content_id", ""), cursor=page.next_cursor),
                                 cursor=page.next_cursor, comments_collected=used)
                    request = self._request(child)
                    if request is not None:
                        yield request
                return
            posts = self.parser.parse_posts(response.body, content_type)
        except BlockedResponse:
            self.terminal_reason = "blocked"
            raise CloseSpider("blocked")
        except SchemaChanged:
            self.terminal_reason = "schema_changed"
            raise CloseSpider("schema_changed")

        if job["kind"] == "detail":
            self._export(posts[:1], job)
            return

        selected = posts[: self.posts_per_target]
        self.seen_posts[job["target_id"]] = len(selected)
        for post in selected:
            content_id = post["content_id"]
            detail_template = job.get("detail_url_template")
            if detail_template:
                child = dict(job, kind="detail", url=detail_template.format(content_id=content_id))
                request = self._request(child)
                if request is not None:
                    yield request
            else:
                self._export([post], job)
            comments_template = job.get("comments_url_template")
            if comments_template:
                child = dict(job, kind="comments", content_id=content_id,
                             url=comments_template.format(content_id=content_id))
                request = self._request(child)
                if request is not None:
                    yield request

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

    def request_failed(self, failure):
        request = failure.request
        job = request.cb_kwargs.get("job", {})
        self.failed_targets.add(str(job.get("target_id") or "unknown"))

    def closed(self, reason):
        report = {
            "records": self.records, "request_count": self.quota.request_count,
            "failed_targets": len(self.failed_targets),
            "quota_exhausted": self.quota.exhausted,
        }
        if self.terminal_reason:
            report["terminal_reason"] = self.terminal_reason
        elif reason not in {"finished", "shutdown"}:
            report["terminal_reason"] = "failed"
            report["message"] = str(reason)
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
