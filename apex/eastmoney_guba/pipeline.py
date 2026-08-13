from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from string import Formatter
from typing import Callable

from .models import CollectionResult
from .targets import EastmoneyTargetProvider


_INTEGER_FIELDS = (
    "lookback_hours", "overlap_hours", "constituent_count",
    "posts_per_sector_forum", "posts_per_constituent_forum", "comments_per_post",
    "concurrent_requests_per_domain", "timeout_seconds", "retry_times",
    "circuit_breaker_failures", "max_requests", "requests_per_target",
    "minimum_posts", "minimum_authors", "collection_timeout_seconds",
)
_ENDPOINT_FIELDS = ("list_url_template", "detail_url_template", "comments_url_template")


def validate_eastmoney_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("eastmoney config must be an object")
    if config.get("access_mode") != "public":
        raise ValueError("eastmoney access_mode must be public")
    if config.get("phase", "shadow") not in {"shadow", "promoted"}:
        raise ValueError("eastmoney phase must be shadow or promoted")
    for name in _INTEGER_FIELDS:
        value = config.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"eastmoney {name} must be a non-negative integer")
        if name in {"timeout_seconds", "collection_timeout_seconds"} and value <= 0:
            raise ValueError(f"eastmoney {name} must be positive")
    for name in ("schema_version", "target_pool_version", "author_salt"):
        if not isinstance(config.get(name), str) or not config[name].strip():
            raise ValueError(f"eastmoney {name} is required")
    if config.get("enabled") and config["author_salt"].strip().upper().startswith("CHANGE-ME"):
        raise ValueError("eastmoney author_salt placeholder must be replaced when enabled")
    delay = config.get("download_delay_seconds")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0:
        raise ValueError("eastmoney download_delay_seconds must be non-negative")
    rate = config.get("minimum_parse_success_rate")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
        raise ValueError("eastmoney minimum_parse_success_rate must be between 0 and 1")
    endpoints = config.get("endpoints")
    if not isinstance(endpoints, dict):
        raise ValueError("eastmoney endpoints are required")
    for name in _ENDPOINT_FIELDS:
        template = endpoints.get(name)
        if not isinstance(template, str) or not template.startswith("https://"):
            raise ValueError(f"eastmoney {name} must be an explicit HTTPS template")
        fields = {field for _, field, _, _ in Formatter().parse(template) if field}
        expected = ({"forum_id"} if name == "list_url_template"
                    else {"content_id"} if name == "comments_url_template"
                    else {"forum_id", "content_id"})
        if not expected.issubset(fields):
            raise ValueError(f"eastmoney {name} is missing placeholders")
    return dict(config)


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _read_cursor(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _valid_pointer(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        batch_path = Path(value["batch_path"])
        if not value.get("batch_id") or not batch_path.is_dir():
            return None
        return value
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _recover_latest_success(cache_dir: Path) -> dict | None:
    candidates = []
    for telemetry_path in (cache_dir / "raw").glob("*/eastmoney/*/telemetry.json"):
        try:
            value = json.loads(telemetry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("status") in {"ok", "empty_valid"} and value.get("batch_id"):
            candidates.append((str(value.get("as_of") or ""), {
                "batch_id": value["batch_id"], "batch_path": str(telemetry_path.parent),
                "status": value["status"],
            }))
    return max(candidates, default=("", None))[1]


def build_frozen_manifest(
    path: Path, *, config: dict, taxonomy: list[dict], constituents: dict[str, list[dict]],
    trade_date: str, collected_at: str, output_file: Path, cursor_file: Path,
) -> dict:
    config = validate_eastmoney_config(config)
    provider = EastmoneyTargetProvider(config["constituent_count"], config["target_pool_version"])
    targets, missing = provider.build_targets(taxonomy, constituents, generated_at=collected_at)
    cursor = _read_cursor(cursor_file)
    floor = _iso(collected_at) - timedelta(
        hours=config["lookback_hours"] + config["overlap_hours"]
    )
    if cursor.get("max_published_at"):
        try:
            cursor_floor = _iso(str(cursor["max_published_at"])) - timedelta(
                hours=config["overlap_hours"]
            )
            floor = max(floor, cursor_floor)
        except ValueError:
            cursor = {}
    endpoints = config["endpoints"]
    jobs = []
    for target in targets:
        values = asdict(target)
        jobs.append({
            "kind": "list", "target_id": target.forum_id,
            "url": endpoints["list_url_template"].format(forum_id=target.forum_id),
            "detail_url_template": endpoints["detail_url_template"].replace(
                "{forum_id}", target.forum_id
            ),
            "comments_url_template": endpoints["comments_url_template"].replace(
                "{forum_id}", target.forum_id
            ),
            "window_start": floor.isoformat(),
            "posts_limit": (config["posts_per_sector_forum"]
                            if target.source_type == "sector_forum"
                            else config["posts_per_constituent_forum"]),
            **values,
        })
    stable = json.dumps({"date": trade_date, "targets": jobs,
                         "schema": config["schema_version"]}, sort_keys=True)
    batch_id = hashlib.sha256((stable + "\0" + path.parent.name).encode()).hexdigest()[:20]
    manifest = {
        "trade_date": trade_date, "batch_id": batch_id, "collected_at": collected_at,
        "window_start": floor.isoformat(), "cursor": cursor,
        "output_file": str(output_file), "author_salt": config["author_salt"],
        "raw_response_dir": str(path.parent / "responses"),
        "jobdir": str(path.parent / "jobdir"),
        "schema_version": config["schema_version"],
        "target_pool_version": config["target_pool_version"],
        "missing_targets": missing,
        "settings": {
            "max_requests": config["max_requests"],
            "requests_per_target": config["requests_per_target"],
            "posts_per_target": max(config["posts_per_sector_forum"],
                                    config["posts_per_constituent_forum"]),
            "comments_per_post": config["comments_per_post"],
            "download_delay_seconds": config["download_delay_seconds"],
            "timeout_seconds": config["timeout_seconds"], "retry_times": config["retry_times"],
            "concurrent_requests_per_domain": config["concurrent_requests_per_domain"],
            "circuit_breaker_failures": config["circuit_breaker_failures"],
        },
        "jobs": jobs,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
    return manifest


def grouped_coverage(short_video_jobs: list[dict], eastmoney: dict, config: dict) -> dict:
    short_ok = any(
        item.get("platform") in {"bili", "dy"}
        and item.get("status") in {"ok", "budget_reused"}
        for item in short_video_jobs
    )
    finance_ok = (
        eastmoney.get("status") in {"ok", "empty_valid"}
        and int(eastmoney.get("posts") or 0) >= config["minimum_posts"]
        and int(eastmoney.get("independent_authors") or 0) >= config["minimum_authors"]
        and float(eastmoney.get("parse_success_rate") or 0) >= config["minimum_parse_success_rate"]
    )
    return {"short_video": short_ok, "finance_community": finance_ok,
            "qualified": short_ok and finance_ok}


def collector_health(report: dict) -> dict:
    parser_successes = int(report.get("parser_successes") or 0)
    parser_errors = int(report.get("parser_errors") or 0)
    request_successes = int(report.get("request_successes") or 0)
    request_errors = int(report.get("request_errors") or 0)
    parser_total = parser_successes + parser_errors
    request_total = request_successes + request_errors
    return {
        "parser_successes": parser_successes, "parser_errors": parser_errors,
        "request_successes": request_successes, "request_errors": request_errors,
        "parse_success_rate": parser_successes / parser_total if parser_total else 0.0,
        "request_success_rate": request_successes / request_total if request_total else 0.0,
    }


def collect_eastmoney(
    *, cache_dir: Path, config: dict, taxonomy: list[dict], constituents: dict[str, list[dict]],
    trade_date: str, collected_at: str, runner: Callable,
) -> tuple[dict, dict]:
    platform_root = cache_dir / "raw" / trade_date / "eastmoney"
    pointer_root = cache_dir / "raw" / "eastmoney"
    batch_root = platform_root / uuid.uuid4().hex
    cursor_file = cache_dir / "cursors" / "eastmoney.json"
    output = batch_root / "records.jsonl"
    manifest_path = batch_root / "manifest.json"
    report_path = batch_root / "collector-report.json"
    manifest = build_frozen_manifest(
        manifest_path, config=config, taxonomy=taxonomy, constituents=constituents,
        trade_date=trade_date, collected_at=collected_at, output_file=output,
        cursor_file=cursor_file,
    )
    try:
        result = runner(manifest_path, report_path,
                        timeout_seconds=config["collection_timeout_seconds"])
    except TypeError:
        result = runner(manifest_path, report_path, config["collection_timeout_seconds"])
    records = []
    if output.exists():
        for line in output.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    posts = sum(not row.get("comment_id") for row in records)
    authors = len({row.get("author_hash") for row in records if row.get("author_hash")})
    sector_forum_records = sum(
        row.get("source_type") == "sector_forum" for row in records
    )
    constituent_forum_records = sum(
        row.get("source_type") == "constituent_forum" for row in records
    )
    collector = {}
    if report_path.exists():
        try:
            collector = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            collector = {}
    requests = int(getattr(result, "request_count", 0))
    failed = int(getattr(result, "failed_targets", 0))
    health = collector_health(collector)
    telemetry = {
        "platform": "eastmoney", "status": result.status, "records": len(records),
        "current_status": result.status, "display_status": result.status,
        "current_path": str(output), "display_path": str(output),
        "posts": posts, "comments": len(records) - posts, "independent_authors": authors,
        "sector_forum_records": sector_forum_records,
        "constituent_forum_records": constituent_forum_records,
        "request_count": requests, "failed_targets": failed,
        "quota_exhausted": bool(getattr(result, "quota_exhausted", False)),
        "circuit_open": result.status == "schema_changed",
        **health,
        "batch_id": manifest["batch_id"], "manifest_path": str(manifest_path),
        "path": str(output), "as_of": collected_at,
        "message": getattr(result, "message", None), "collector": collector,
    }
    pointer_root.mkdir(parents=True, exist_ok=True)
    latest = pointer_root / "latest.json"
    _atomic_json(latest, {"batch_id": manifest["batch_id"],
                          "batch_path": str(batch_root), "status": result.status})
    if result.status in {"ok", "empty_valid"}:
        _atomic_json(pointer_root / "latest-success.json", {
            "batch_id": manifest["batch_id"], "batch_path": str(batch_root),
            "status": result.status,
        })
    else:
        previous = (_valid_pointer(pointer_root / "latest-success.json")
                    or _recover_latest_success(cache_dir))
    if result.status not in {"ok", "empty_valid"} and previous:
        telemetry["stale"] = True
        telemetry["stale_from_batch_id"] = previous["batch_id"]
        telemetry["display_status"] = "stale"
        telemetry["display_path"] = str(Path(previous["batch_path"]) / "records.jsonl")
        previous_telemetry = Path(previous["batch_path"]) / "telemetry.json"
        if previous_telemetry.exists():
            telemetry["last_success"] = json.loads(
                previous_telemetry.read_text(encoding="utf-8")
            )
            telemetry["display_report"] = telemetry["last_success"]
    telemetry_path = batch_root / "telemetry.json"
    with telemetry_path.open("x", encoding="utf-8") as stream:
        json.dump(telemetry, stream, ensure_ascii=False, sort_keys=True, indent=2)
    if result.status in {"ok", "empty_valid"} and records:
        dated = [(row.get("published_at"), str(row.get("content_id") or "")) for row in records
                 if row.get("published_at")]
        if dated:
            maximum = max(dated)
            cursor_file.parent.mkdir(parents=True, exist_ok=True)
            cursor_file.write_text(json.dumps({"max_published_at": maximum[0],
                                               "max_content_id": maximum[1]}, sort_keys=True),
                                   encoding="utf-8")
    return telemetry, {"trade_date": trade_date, "jobs": [{
        "platform": "eastmoney", "mode": "eastmoney_guba",
        "source_id": f"eastmoney:{manifest['batch_id']}", "sector_ids": [],
        "status": "ok" if result.status in {"ok", "partial"} and output.exists() else result.status,
        "records": len(records), "path": str(output),
    }]}
