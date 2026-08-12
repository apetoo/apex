from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .models import CollectionResult


class EastmoneyRunner:
    """Run the isolated Scrapy entry point and classify its machine-readable report."""

    @staticmethod
    def classify_result(report: dict) -> CollectionResult:
        records = int(report.get("records") or 0)
        requests = int(report.get("request_count") or 0)
        failed = int(report.get("failed_targets") or 0)
        quota = bool(report.get("quota_exhausted"))
        terminal = report.get("terminal_reason")
        if terminal in {"blocked", "schema_changed", "failed"}:
            status = terminal
        elif failed or quota:
            status = "partial" if records else "failed"
        elif records:
            status = "ok"
        else:
            status = "empty_valid"
        return CollectionResult(status, records, requests, failed, quota, report.get("message"))

    def run(self, job_file: Path, report_file: Path, *, timeout_seconds: int) -> CollectionResult:
        command = [sys.executable, "-m", "apex.eastmoney_guba.spider", "--jobs", str(job_file),
                   "--report", str(report_file)]
        try:
            completed = subprocess.run(command, check=False, timeout=timeout_seconds,
                                       capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            return CollectionResult("failed", 0, 0, 1, message="collector timeout")
        if completed.returncode or not report_file.exists():
            return CollectionResult("failed", 0, 0, 1, message="collector process failed")
        return self.classify_result(json.loads(report_file.read_text(encoding="utf-8")))
