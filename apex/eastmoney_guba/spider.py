"""Scrapy process boundary for the public Eastmoney Guba collector.

The pipeline integration supplies frozen job manifests. This module deliberately
does not load login state, cookies, JavaScript engines, proxy pools, or CAPTCHA solvers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    import scrapy
    from scrapy.crawler import CrawlerProcess
except ImportError:  # pragma: no cover - exercised in a dependency-installed runtime
    scrapy = None
    CrawlerProcess = None


if scrapy is not None:
    class EastmoneySpider(scrapy.Spider):
        name = "eastmoney_guba"
        allowed_domains = ["eastmoney.com"]
        custom_settings = {
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "DOWNLOAD_DELAY": 2.0,
            "RANDOMIZE_DOWNLOAD_DELAY": True,
            "AUTOTHROTTLE_ENABLED": True,
            "COOKIES_ENABLED": False,
            "RETRY_HTTP_CODES": [429, 500, 502, 503, 504],
            "USER_AGENT": "ApexLocalResearch/1.0 (public, low-frequency research collector)",
        }

        def __init__(self, jobs: list[dict], report_path: str, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.jobs = jobs
            self.report_path = Path(report_path)
            self.records = 0

        def start_requests(self):
            for job in self.jobs:
                yield scrapy.Request(job["url"], callback=self.parse, dont_filter=False)

        def parse(self, response):
            self.records += 1
            yield {"url": response.url, "status": response.status, "body": response.body}

        def closed(self, reason):
            self.report_path.write_text(json.dumps({
                "records": self.records,
                "request_count": self.crawler.stats.get_value("downloader/request_count", 0),
                "failed_targets": 0 if reason == "finished" else 1,
                "quota_exhausted": False,
            }), encoding="utf-8")
else:
    class EastmoneySpider:  # type: ignore[no-redef]
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if CrawlerProcess is None:
        raise RuntimeError("Scrapy is required; install Apex project dependencies")
    jobs = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    process = CrawlerProcess()
    process.crawl(EastmoneySpider, jobs=jobs, report_path=args.report)
    process.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
