"""Explicit, Eastmoney-only historical collection boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from apex import sector_sentiment as sentiment

from .pipeline import collect_eastmoney, validate_eastmoney_config
from .representatives import describe_injected_constituents, select_representative_constituents
from .runner import EastmoneyRunner
from .parser import EastmoneyParser
from .exporter import EastmoneyExporter
from .models import CollectionResult


def validate_backfill_request(*, trade_date: str, as_of: str,
                              count_shadow_day: bool = False, reason: str = "",
                              phase: str = "shadow") -> None:
    try:
        date.fromisoformat(trade_date)
    except ValueError as exc:
        raise ValueError("trade_date must be an ISO date") from exc
    try:
        timestamp = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("as_of must be an ISO timestamp") from exc
    if timestamp.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    if count_shadow_day and not reason.strip():
        raise ValueError("counting a shadow day requires an audit reason")
    if count_shadow_day and phase != "shadow":
        raise ValueError("only shadow backfills may count as shadow days")


def _representatives(taxonomy: list[dict], trade_date: str, config: dict,
                     provider: Callable | None) -> tuple[dict, dict]:
    if provider is None:
        return select_representative_constituents(
            taxonomy, trade_date, config["constituent_count"],
        )
    supplied = provider(taxonomy, trade_date)
    if isinstance(supplied, tuple) and len(supplied) == 2:
        constituents, provenance = supplied
        if not isinstance(constituents, dict) or not isinstance(provenance, dict):
            raise ValueError("representative provider returned invalid data")
        return constituents, provenance
    if not isinstance(supplied, dict):
        raise ValueError("representative provider must return a sector mapping")
    return supplied, describe_injected_constituents(
        taxonomy, supplied, trade_date=trade_date,
        constituent_count=config["constituent_count"],
    )


def _saved_batch(cache_dir: Path, trade_date: str) -> tuple[Path, dict]:
    batches = sorted((cache_dir / "raw" / trade_date / "eastmoney").glob("*/manifest.json"),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    for path in batches:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        response_dir = Path(str(manifest.get("raw_response_dir") or ""))
        if not response_dir.is_dir():
            continue
        for job in manifest.get("jobs", []):
            if job.get("kind") != "list":
                continue
            for candidate in response_dir.glob("*.body"):
                body = candidate.read_bytes()
                identity = hashlib.sha256(("list\0" + str(job["url"]) + "\0").encode("utf-8") + body).hexdigest()
                if candidate.name == f"{identity}.body":
                    return path, manifest
    raise FileNotFoundError("offline replay requires a saved Eastmoney batch with list responses")


def _offline_runner(source_manifest: dict) -> Callable:
    """Replay saved list bodies through the current parser, without any network request."""
    source_dir = Path(str(source_manifest["raw_response_dir"]))

    def run(manifest_path: Path, report_path: Path, *, timeout_seconds: int) -> CollectionResult:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        exporter = EastmoneyExporter(Path(manifest["output_file"]), author_salt=manifest["author_salt"])
        parser = EastmoneyParser()
        successes = errors = records = 0
        old_jobs = {str(job["target_id"]): job for job in source_manifest.get("jobs", [])
                    if job.get("kind") == "list"}
        for job in manifest["jobs"]:
            old = old_jobs.get(str(job["target_id"]))
            if old is None:
                errors += 1
                continue
            body_path = None
            for candidate in source_dir.glob("*.body"):
                body = candidate.read_bytes()
                identity = hashlib.sha256(("list\0" + str(old["url"]) + "\0").encode("utf-8") + body).hexdigest()
                if candidate.name == f"{identity}.body":
                    body_path = candidate
                    break
            if body_path is None:
                errors += 1
                continue
            try:
                rows = parser.parse_posts(body_path.read_bytes(), "text/html")
                floor = datetime.fromisoformat(str(job["window_start"]).replace("Z", "+00:00"))
                rows = [row for row in rows if datetime.fromisoformat(
                    str(row["published_at"]).replace("Z", "+00:00")
                ).replace(tzinfo=floor.tzinfo) >= floor]
                exporter.export(rows[:int(job["posts_limit"])], trade_date=manifest["trade_date"],
                    batch_id=manifest["batch_id"], sector_id=job["sector_id"],
                    source_type=job["source_type"], stock_code=job.get("stock_code"),
                    pool_version=job["pool_version"], collected_at=manifest["collected_at"])
                records += min(len(rows), int(job["posts_limit"]))
                successes += 1
            except Exception:
                errors += 1
        report_path.write_text(json.dumps({"records": records, "request_count": 0,
            "failed_targets": errors, "parser_successes": successes, "parser_errors": errors,
            "request_successes": 0, "request_errors": 0}, sort_keys=True), encoding="utf-8")
        return CollectionResult("ok" if successes and not errors else "failed", records, 0, errors)
    return run


def run_eastmoney_backfill(
    cfg: dict, *, trade_date: str, as_of: str, offline_replay: bool = False,
    count_shadow_day: bool = False, reason: str | None = None,
    eastmoney_runner: Callable | None = None,
    eastmoney_constituent_provider: Callable | None = None,
) -> dict:
    """Collect one frozen historical window without running short-video collection or scoring."""
    settings = dict((cfg.get("sector_sentiment") or {}))
    eastmoney = dict(settings.get("eastmoney") or {})
    if not settings.get("enabled") or not eastmoney.get("enabled"):
        raise ValueError("Eastmoney backfill requires enabled sector sentiment and Eastmoney")
    phase = str(eastmoney.get("phase") or "shadow")
    validate_backfill_request(trade_date=trade_date, as_of=as_of,
                              count_shadow_day=count_shadow_day, reason=reason or "", phase=phase)
    validate_eastmoney_config(eastmoney)
    cache_dir = Path(str(settings.get("cache_dir", "~/.stock-sentiment"))).expanduser()
    taxonomy = list(settings.get("taxonomy") or [])
    if not taxonomy:
        raise ValueError("Eastmoney backfill requires an explicit taxonomy")
    if offline_replay:
        _, source_manifest = _saved_batch(cache_dir, trade_date)
        source_provenance = source_manifest.get("representative_constituents") or {}
        constituents = {sector_id: list(detail.get("selected") or [])
                        for sector_id, detail in source_provenance.items()}
        provenance = source_provenance
        runner = _offline_runner(source_manifest)
    else:
        constituents, provenance = _representatives(
            taxonomy, trade_date, eastmoney, eastmoney_constituent_provider,
        )
        runner = eastmoney_runner or EastmoneyRunner().run
    telemetry, jobs = collect_eastmoney(
        cache_dir=cache_dir, config=eastmoney, taxonomy=taxonomy,
        constituents=constituents, representative_provenance=provenance,
        trade_date=trade_date, collected_at=as_of, runner=runner,
    )
    qualified = bool(
        telemetry.get("status") in {"ok", "empty_valid"}
        and int(telemetry.get("posts") or 0) >= int(eastmoney["minimum_posts"])
        and int(telemetry.get("independent_authors") or 0) >= int(eastmoney["minimum_authors"])
        and float(telemetry.get("parse_success_rate") or 0) >= float(eastmoney["minimum_parse_success_rate"])
        and not telemetry.get("target_shortfall")
    )
    counted = bool(count_shadow_day and qualified)
    telemetry.update({
        "backfill": True,
        "phase": "shadow" if counted else "backfill",
        "shadow_qualified": counted,
        "backfill_audit": {
            "count_shadow_day": bool(count_shadow_day),
            "counted_shadow_day": counted,
            "reason": (reason or "").strip() if count_shadow_day else None,
            "as_of": as_of,
        },
    })
    telemetry_path = Path(str(telemetry["path"])).parent / "telemetry.json"
    telemetry_path.write_text(json.dumps(telemetry, ensure_ascii=False, sort_keys=True, indent=2),
                              encoding="utf-8")
    report = {
        "trade_date": trade_date, "coverage": 1.0 if counted else 0.0,
        "search_coverage": 0.0, "creator_coverage": 0.0,
        "platforms": {"eastmoney": telemetry}, "funnel": None,
    }
    sentiment.open_store(cache_dir).record_collection(report)
    return {"backfill": True, "counted_shadow_day": counted, "scored": False,
            "collection": {"eastmoney": telemetry, "jobs": jobs["jobs"]}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Eastmoney Guba historical backfill")
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--count-shadow-day", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--offline-replay", action="store_true")
    args = parser.parse_args(argv)
    from apex import config
    result = run_eastmoney_backfill(config.get() or {}, trade_date=args.trade_date,
        as_of=args.as_of, offline_replay=args.offline_replay,
        count_shadow_day=args.count_shadow_day, reason=args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
