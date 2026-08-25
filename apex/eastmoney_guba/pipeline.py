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
from .representatives import (
    describe_injected_constituents,
    normalize_injected_representative_provenance,
)
from .targets import EastmoneyTargetProvider


_INTEGER_FIELDS = (
    "lookback_hours", "overlap_hours", "constituent_count",
    "posts_per_sector_forum", "posts_per_constituent_forum", "comments_per_post",
    "concurrent_requests_per_domain", "timeout_seconds", "retry_times",
    "circuit_breaker_failures", "max_requests", "requests_per_target",
    "minimum_posts", "minimum_authors", "collection_timeout_seconds",
)
_ENDPOINT_FIELDS = (
    "list_url_template", "list_next_url_template",
    "detail_url_template", "comments_url_template",
)


def validate_eastmoney_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("eastmoney config must be an object")
    if config.get("access_mode") != "public":
        raise ValueError("eastmoney access_mode must be public")
    if config.get("phase", "shadow") not in {"shadow", "promoted"}:
        raise ValueError("eastmoney phase must be shadow or promoted")
    override_reason = config.get("promotion_override_reason", "")
    if not isinstance(override_reason, str):
        raise ValueError("eastmoney promotion_override_reason must be a string")
    if len(override_reason.strip()) > 500:
        raise ValueError("eastmoney promotion_override_reason is too long")
    for name in _INTEGER_FIELDS:
        value = config.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"eastmoney {name} must be a non-negative integer")
        if name in {"timeout_seconds", "collection_timeout_seconds"} and value <= 0:
            raise ValueError(f"eastmoney {name} must be positive")
    max_comment_requests = config.get("max_comment_requests", min(200, config["max_requests"]))
    if (isinstance(max_comment_requests, bool) or not isinstance(max_comment_requests, int)
            or max_comment_requests < 0):
        raise ValueError("eastmoney max_comment_requests must be a non-negative integer")
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
        expected = ({"forum_id", "page"} if name == "list_next_url_template"
                    else {"forum_id"} if name == "list_url_template"
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
    representative_provenance: dict[str, dict] | None = None,
) -> dict:
    config = validate_eastmoney_config(config)
    provider = EastmoneyTargetProvider(config["constituent_count"], config["target_pool_version"])
    targets, missing = provider.build_targets(taxonomy, constituents, generated_at=collected_at)
    supplied_provenance = dict(representative_provenance or {})
    representative_provenance = (
        normalize_injected_representative_provenance(
            taxonomy, constituents, supplied_provenance, trade_date=trade_date,
            constituent_count=config["constituent_count"],
        ) if supplied_provenance else describe_injected_constituents(
            taxonomy, constituents, trade_date=trade_date,
            constituent_count=config["constituent_count"],
        )
    )
    for sector_id, detail in representative_provenance.items():
        requested = int(detail.get("requested_count") or config["constituent_count"])
        selected = int(detail.get("selected_count") or 0)
        if detail.get("status") != "ok" or selected < requested:
            missing.append({
                "sector_id": str(sector_id), "reason": "target_shortfall",
                "requested_count": requested, "selected_count": selected,
            })
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
            "list_next_url_template": endpoints["list_next_url_template"].replace(
                "{forum_id}", target.forum_id
            ),
            "detail_url_template": endpoints["detail_url_template"].replace(
                "{forum_id}", target.forum_id
            ),
            "comments_url_template": endpoints["comments_url_template"].replace(
                "{forum_id}", target.forum_id
            ),
            "window_start": floor.isoformat(),
            "page": 1,
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
        "representative_constituents": representative_provenance,
        "missing_targets": missing,
        "settings": {
            "max_requests": config["max_requests"],
            "max_comment_requests": config.get(
                "max_comment_requests", min(200, config["max_requests"]),
            ),
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
    business_total = parser_successes + parser_errors + request_errors
    return {
        "parser_successes": parser_successes, "parser_errors": parser_errors,
        "request_successes": request_successes, "request_errors": request_errors,
        "parse_success_rate": parser_successes / parser_total if parser_total else 0.0,
        "request_success_rate": request_successes / request_total if request_total else 0.0,
        "business_success_rate": parser_successes / business_total if business_total else 0.0,
    }


_SAFE_COLLECTOR_FIELDS = (
    "records", "request_count", "failed_targets", "quota_exhausted",
    "parser_successes", "parser_errors", "request_successes", "request_errors",
    "terminal_reason",
    "comment_request_count", "comment_quota_exhausted", "comment_source",
    "comment_fallback_used", "comment_circuit_open", "comment_source_requests",
    "comment_source_successes", "comment_source_failures",
)


def _safe_collector_report(report: dict) -> dict:
    """Keep only typed collector fields that are safe to freeze into telemetry."""
    return {key: report[key] for key in _SAFE_COLLECTOR_FIELDS if key in report}


def _safe_result_message(result) -> str | None:
    if getattr(result, "message", None) is None:
        return None
    status = str(getattr(result, "status", "") or "unavailable")
    return f"collector_{status}" if status.isidentifier() else "collector_unavailable"


def collect_eastmoney(
    *, cache_dir: Path, config: dict, taxonomy: list[dict], constituents: dict[str, list[dict]],
    trade_date: str, collected_at: str, runner: Callable,
    representative_provenance: dict[str, dict] | None = None,
    promotion_audit: dict | None = None,
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
        cursor_file=cursor_file, representative_provenance=representative_provenance,
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
    collector = _safe_collector_report(collector) if isinstance(collector, dict) else {}
    if report_path.exists():
        report_path.write_text(json.dumps(collector, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    requests = int(getattr(result, "request_count", 0))
    failed = int(getattr(result, "failed_targets", 0))
    health = collector_health(collector)
    target_shortfall = any(
        item.get("reason") == "target_shortfall" for item in manifest["missing_targets"]
    )
    provider_unavailable = any(
        str(item.get("status") or "") == "failed"
        and str(item.get("reason") or "").startswith("representative provider failed:")
        for item in manifest["representative_constituents"].values()
    )
    effective_status = "degraded" if target_shortfall else result.status
    telemetry = {
        "platform": "eastmoney", "status": effective_status, "records": len(records),
        "current_status": effective_status, "display_status": effective_status,
        "current_path": str(output), "display_path": str(output),
        "posts": posts, "comments": len(records) - posts, "independent_authors": authors,
        "sector_forum_records": sector_forum_records,
        "constituent_forum_records": constituent_forum_records,
        "request_count": requests, "failed_targets": failed,
        "quota_exhausted": bool(getattr(result, "quota_exhausted", False)),
        "comment_source": str(collector.get("comment_source") or "primary"),
        "comment_fallback_used": bool(collector.get("comment_fallback_used")),
        "comment_circuit_open": bool(collector.get("comment_circuit_open")),
        "comment_request_count": int(collector.get("comment_request_count") or 0),
        "comment_quota_exhausted": bool(collector.get("comment_quota_exhausted")),
        "comment_source_requests": dict(collector.get("comment_source_requests") or {}),
        "comment_source_successes": dict(collector.get("comment_source_successes") or {}),
        "comment_source_failures": dict(collector.get("comment_source_failures") or {}),
        "circuit_open": result.status == "schema_changed", "target_shortfall": target_shortfall,
        "representative_constituents": manifest["representative_constituents"],
        **health,
        "batch_id": manifest["batch_id"], "manifest_path": str(manifest_path),
        "path": str(output), "as_of": collected_at,
        "message": (_safe_result_message(result)
                    or ("representative_provider_unavailable" if provider_unavailable else None)),
        "collector": collector,
    }
    if promotion_audit is not None:
        telemetry["promotion_audit"] = dict(promotion_audit)
    pointer_root.mkdir(parents=True, exist_ok=True)
    latest = pointer_root / "latest.json"
    _atomic_json(latest, {"batch_id": manifest["batch_id"],
                          "batch_path": str(batch_root), "status": effective_status})
    if effective_status in {"ok", "empty_valid"}:
        _atomic_json(pointer_root / "latest-success.json", {
            "batch_id": manifest["batch_id"], "batch_path": str(batch_root),
            "status": effective_status,
        })
    else:
        previous = (_valid_pointer(pointer_root / "latest-success.json")
                    or _recover_latest_success(cache_dir))
    if effective_status not in {"ok", "empty_valid"} and previous:
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
    if effective_status in {"ok", "empty_valid"} and records:
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
        # Target shortfall degrades quality/promotion, but successfully collected raw
        # rows still enter the audit store (and remain score-isolated in shadow mode).
        "status": ("ok" if result.status in {"ok", "partial"} and output.exists()
                   else result.status),
        "records": len(records), "path": str(output),
    }]}
