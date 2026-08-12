"""Scrapy process boundary for public, unauthenticated Eastmoney Guba jobs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import CloseSpider

from .exporter import EastmoneyExporter
from .models import QuotaBudget
from .parser import BlockedResponse, EastmoneyParser, SchemaChanged


_KINDS = {"list", "detail", "comments"}


def _validate_manifest(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
        raise ValueError("manifest requires a jobs list")
    required = ("trade_date", "batch_id", "collected_at", "output_file", "author_salt")
    if any(not value.get(key) for key in required):
        raise ValueError("manifest missing required batch/export fields")
    for job in value["jobs"]:
        if not isinstance(job, dict) or job.get("kind") not in _KINDS or not job.get("url"):
            raise ValueError("each job requires a typed kind and URL")
        for key in ("target_id", "sector_id", "source_type", "pool_version"):
            if not job.get(key):
                raise ValueError(f"job missing {key}")
    return value


class EastmoneySpider(scrapy.Spider):
    name = "eastmoney_guba"
    handle_httpstatus_list = [403]
    custom_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "AUTOTHROTTLE_ENABLED": True,
        "COOKIES_ENABLED": False,
        "LOG_LEVEL": "WARNING",
        "LOGSTATS_INTERVAL": 0,
        "RETRY_HTTP_CODES": [429, 500, 502, 503, 504],
        "RETRY_PRIORITY_ADJUST": -1,
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy.downloadermiddlewares.retry.RetryMiddleware": 550,
        },
        "USER_AGENT": "ApexLocalResearch/1.0 (public, low-frequency research collector)",
    }

    def __init__(self, manifest: dict, report_path: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.manifest = _validate_manifest(manifest)
        self.report_path = Path(report_path)
        settings = manifest.get("settings") or {}
        self.quota = QuotaBudget(int(settings.get("max_requests", 1000)),
                                 int(settings.get("requests_per_target", 100)))
        self.posts_per_target = int(settings.get("posts_per_target", 100))
        self.comments_per_post = int(settings.get("comments_per_post", 50))
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
        target_id = str(job["target_id"])
        if not self.quota.consume(target_id):
            return None
        return scrapy.Request(job["url"], callback=self.parse_job, errback=self.request_failed,
                              cb_kwargs={"job": job}, dont_filter=False)

    def parse_job(self, response, job: dict):
        if response.status == 403:
            self.terminal_reason = "blocked"
            raise CloseSpider("blocked")
        content_type = response.headers.get(b"Content-Type", b"").decode("latin1")
        try:
            if job["kind"] == "comments":
                records = self.parser.parse_comments(response.body, content_type)
                records = records[: self.comments_per_post]
                self._export(records, job)
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
                child = dict(job, kind="comments", url=comments_template.format(content_id=content_id))
                request = self._request(child)
                if request is not None:
                    yield request

    def _export(self, records: list[dict], job: dict):
        self.records += self.exporter.export(
            records, trade_date=self.manifest["trade_date"], batch_id=self.manifest["batch_id"],
            sector_id=job["sector_id"], source_type=job["source_type"],
            stock_code=job.get("stock_code"), pool_version=job["pool_version"],
            collected_at=self.manifest["collected_at"],
        )

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


def main() -> int:
    cli = argparse.ArgumentParser()
    cli.add_argument("--jobs", required=True)
    cli.add_argument("--report", required=True)
    args = cli.parse_args()
    manifest = _validate_manifest(json.loads(Path(args.jobs).read_text(encoding="utf-8")))
    configured = manifest.get("settings") or {}
    domains = {urlparse(job["url"]).hostname for job in manifest["jobs"]}
    process_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": int(configured.get("concurrent_requests_per_domain", 1)),
        "DOWNLOAD_DELAY": float(configured.get("download_delay_seconds", 2)),
        "DOWNLOAD_TIMEOUT": int(configured.get("timeout_seconds", 20)),
        "RETRY_TIMES": int(configured.get("retry_times", 3)),
        "JOBDIR": manifest.get("jobdir"),
        "TELNETCONSOLE_ENABLED": False,
        "ROBOTSTXT_OBEY": False,
    }
    # RetryMiddleware provides bounded retries; AutoThrottle plus randomized delay
    # supplies increasing inter-request pressure without high-frequency retry loops.
    process = CrawlerProcess(settings=process_settings)
    process.crawl(EastmoneySpider, manifest=manifest, report_path=args.report,
                  allowed_domains=sorted(domain for domain in domains if domain))
    process.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
