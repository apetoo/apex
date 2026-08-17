"""Sector-level retail sentiment shadow alerts.

This module owns normalized storage, deterministic scoring state and external
crawler boundaries. Platform-specific crawling remains in separately installed
tools (for example MediaCrawler); Apex consumes their JSONL output only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import sqlite3
import subprocess
import uuid
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from apex.mediacrawler_bounded import ADAPTER_VERSION
from apex.sector_retrieval import (
    CREATOR_RULE_VERSION,
    EXCLUDE_TERMS,
    FINANCE_TERMS,
    QUERY_VERSION,
    RELEVANCE_VERSION,
    RetrievalJob,
    RelevanceDecision,
    build_creator_jobs,
    build_search_jobs,
    classify_financial_relevance,
)


MODEL_VERSION = "sector-sentiment-heuristic-v1"
RULE_VERSION = "sector-alert-v1"
PROMPT_VERSION = "sector-semantic-v1"
DICTIONARY_VERSION = "sector-taxonomy-v1"
MAPPING_VERSION = "sector-alias-v1"
MIN_PLATFORMS = 2
MIN_AUTHORS = 10
MIN_MAPPING_CONFIDENCE = 0.70
EASTMONEY_PROMOTION_QUALIFIED_DAYS = 14


class CreatorNotFoundError(Exception):
    """Raised when moderation targets a creator absent from the audited pool."""

    def __init__(self, platform: str, creator_id: str):
        self.platform = platform
        self.creator_id = creator_id
        super().__init__("creator not found")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(text: str) -> str:
    normalized = re.sub(r"\W+", "", (text or "").lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _policy_cohort_id(config_hash: str, start_date: str) -> str:
    return hashlib.sha256(f"{config_hash}:{start_date}".encode("utf-8")).hexdigest()[:24]


def _redact_text(value: Any, limit: int | None = None) -> str:
    """Remove contact handles from persisted/API evidence while keeping semantics."""
    text = str(value or "")
    text = re.sub(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "[邮箱已脱敏]", text,
    )
    text = re.sub(
        r"(?<!\d)(?:\+?86[-\s]?)?1[3-9](?:[-\s]?\d){9}(?!\d)",
        "[电话已脱敏]", text,
    )
    text = re.sub(
        r"(?<!\d)0\d{2,3}[-\s]\d{7,8}(?!\d)", "[电话已脱敏]", text,
    )
    text = re.sub(
        r"(?i)(?:https?://)?(?:weibo\.com|t\.me|douyin\.com/user|"
        r"xiaohongshu\.com/user|space\.bilibili\.com)/?[^\s,，。；;]*",
        "[社交链接已脱敏]", text,
    )
    text = re.sub(
        r"(?i)(微信|wechat|wx|qq|抖音号|小红书号|b站uid|uid|公众号|微博|"
        r"weibo|telegram|tg|知乎号)\s*[:：号]?\s*[^\s,，。；;、]{2,}",
        r"\1[账号已脱敏]", text,
    )
    text = re.sub(r"@[\w.-]{2,}", "@[账号已脱敏]", text)
    return text if limit is None else text[:limit]


def _semantic_text(record: dict) -> str:
    """Build the redacted classification input without dropping retrieval metadata."""
    tags = record.get("tags") or []
    if isinstance(tags, (list, tuple, set)):
        tags = " ".join(str(tag) for tag in tags)
    parts = [
        str(record.get("title") or ""),
        str(record.get("description") or ""),
        str(tags or ""),
        str(record.get("text") or ""),
    ]
    return _redact_text("\n".join(dict.fromkeys(part for part in parts if part)))


def _json_list(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, tuple):
        return list(value)
    return value if isinstance(value, list) else []


def _sanitize_creator_evidence(value: Any) -> list[dict]:
    allowed = (
        "text", "published_at", "relevance_score", "reasons",
        "query_version", "relevance_version", "creator_rule_version", "config_hash",
    )
    output = []
    for item in _json_list(value):
        if not isinstance(item, dict):
            continue
        sanitized = {key: item[key] for key in allowed if key in item}
        if "text" in sanitized:
            sanitized["text"] = _redact_text(sanitized["text"], 200)
        if "reasons" in sanitized:
            sanitized["reasons"] = [
                _redact_text(reason, 200) for reason in _json_list(sanitized["reasons"])
            ]
        output.append(sanitized)
    return output


def _sanitize_score_evidence(value: Any) -> list[dict]:
    output = []
    for item in _json_list(value):
        if not isinstance(item, dict):
            continue
        sanitized = {
            key: item[key] for key in ("platform", "text", "stance") if key in item
        }
        url = str(item.get("url") or "")
        if (item.get("platform") == "eastmoney"
                and url.startswith("https://guba.eastmoney.com/")):
            sanitized["url"] = url
        if "text" in sanitized:
            sanitized["text"] = _redact_text(sanitized["text"], 160)
        output.append(sanitized)
    return output


def _parse_timestamp(value: Any) -> datetime:
    """Parse canonical ISO timestamps and common MediaCrawler epoch values."""
    if value is None or isinstance(value, bool):
        raise ValueError("timestamp is missing")
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000
        parsed = datetime.fromtimestamp(numeric, tz=timezone.utc)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        raise ValueError("unsupported timestamp type")
    return parsed.astimezone(timezone.utc)


def _normalized_timestamp(value: Any) -> str:
    return _parse_timestamp(value).isoformat(timespec="seconds")


def _creator_cutoff_reason(record: dict, cutoff: str | None) -> str | None:
    if not cutoff:
        return "creator_missing_approval_cutoff"
    published_at = record.get("published_at")
    if published_at in (None, ""):
        return "creator_missing_published_at"
    try:
        published = _parse_timestamp(published_at)
    except (TypeError, ValueError, OverflowError, OSError):
        return "creator_invalid_published_at"
    try:
        approved = _parse_timestamp(cutoff)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError("creator job has an invalid approval cutoff") from exc
    if published <= approved:
        return "creator_before_approval"
    return None


def _creator_parent_cutoffs(records: list[dict], cutoff: str | None) -> dict[str, str | None]:
    """Resolve each creator content root once so comments cannot revive old posts."""
    roots: dict[str, str | None] = {}
    for record in records:
        if str(record.get("comment_id") or ""):
            continue
        content_id = str(record.get("content_id") or "")
        if not content_id:
            continue
        reason = _creator_cutoff_reason(record, cutoff)
        if content_id not in roots or reason is None:
            roots[content_id] = reason
    return roots


def _creator_record_cutoff_reason(
    record: dict, cutoff: str | None, parent_cutoffs: dict[str, str | None],
) -> str | None:
    """Apply the approval cutoff to both a row and its canonical parent content."""
    if str(record.get("comment_id") or ""):
        content_id = str(record.get("content_id") or "")
        if content_id not in parent_cutoffs:
            return "creator_missing_parent_content"
        parent_reason = parent_cutoffs[content_id]
        if parent_reason:
            return "creator_parent_" + parent_reason.removeprefix("creator_")
    return _creator_cutoff_reason(record, cutoff)


def _validate_retrieval_policy(settings: dict, retrieval_settings: dict) -> str:
    """Validate frozen versions and return the canonical audit hash."""
    commit = str(settings.get("mediacrawler_commit") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ValueError("sector_sentiment.mediacrawler_commit must be a full 40-hex pin")
    expected = {
        "query_version": QUERY_VERSION,
        "relevance_version": RELEVANCE_VERSION,
        "creator_rule_version": CREATOR_RULE_VERSION,
    }
    for key, required in expected.items():
        actual = retrieval_settings.get(key)
        if actual != required:
            raise ValueError(f"retrieval.{key} must equal frozen version {required!r}")
    if settings.get("semantic_prompt_version") != PROMPT_VERSION:
        raise ValueError(
            f"sector_sentiment.semantic_prompt_version must equal {PROMPT_VERSION!r}"
        )
    relevance_llm_enabled = bool(retrieval_settings.get(
        "relevance_llm_enabled", settings.get("llm_enabled", False),
    ))
    if relevance_llm_enabled and not settings.get("relevance_llm_model"):
        raise ValueError("sector_sentiment.relevance_llm_model is required when enabled")
    if settings.get("llm_enabled", False) and not settings.get("semantic_llm_model"):
        raise ValueError("sector_sentiment.semantic_llm_model is required when enabled")
    eastmoney = settings.get("eastmoney") or {}
    eastmoney_promoted = bool(eastmoney.get("enabled") and
                              eastmoney.get("phase", "shadow") == "promoted")
    frozen_platforms = [value for value in settings.get("platforms", ["bili", "dy"])
                        if value != "eastmoney" or eastmoney_promoted]
    frozen = {
        **expected,
        "mediacrawler_commit": commit.lower(),
        "platforms": frozen_platforms,
        "taxonomy": settings.get("taxonomy"),
        "fallback_keywords": settings.get("keywords"),
        "llm_enabled": bool(settings.get("llm_enabled", False)),
        "relevance_llm_enabled": relevance_llm_enabled,
        "relevance_llm_model": settings.get("relevance_llm_model"),
        "semantic_llm_model": settings.get("semantic_llm_model"),
        "semantic_prompt_version": PROMPT_VERSION,
        "semantic_model_version": settings.get("llm_model_version"),
        "collector_adapter_version": ADAPTER_VERSION,
        "query_templates": retrieval_settings.get("query_templates"),
        "max_queries_per_sector": retrieval_settings.get("max_queries_per_sector"),
        "max_contents_per_query": retrieval_settings.get("max_contents_per_query"),
        "max_comments_per_content": retrieval_settings.get("max_comments_per_content"),
        "candidate_min_contents": retrieval_settings.get("candidate_min_contents"),
        "candidate_min_sectors": retrieval_settings.get("candidate_min_sectors"),
        "very_high_relevance": retrieval_settings.get("very_high_relevance"),
        "high_engagement_thresholds": retrieval_settings.get("high_engagement_thresholds"),
        "platform_engagement_thresholds": retrieval_settings.get(
            "platform_engagement_thresholds"
        ),
        "creator_id_argument": retrieval_settings.get("creator_id_argument", "--creator_id"),
        "finance_terms": list(FINANCE_TERMS),
        "exclude_terms": list(EXCLUDE_TERMS),
    }
    if eastmoney_promoted:
        # The operator's audit note authorizes rollout; it does not change the
        # frozen retrieval/scoring policy and therefore must not rotate its hash.
        frozen["eastmoney"] = {
            key: value for key, value in eastmoney.items()
            if key != "promotion_override_reason"
        }
    payload = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SentimentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS content (
                    id INTEGER PRIMARY KEY,
                    platform TEXT NOT NULL, content_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL DEFAULT '', published_at TEXT,
                    collected_at TEXT NOT NULL, trade_date TEXT, text TEXT NOT NULL,
                    semantic_text TEXT NOT NULL DEFAULT '',
                    engagement REAL NOT NULL DEFAULT 0, url TEXT, batch_id TEXT,
                    author_hash TEXT, fingerprint TEXT NOT NULL,
                    repost_weight REAL NOT NULL DEFAULT 1.0,
                    relevance_score REAL,
                    relevance_decision TEXT NOT NULL DEFAULT 'unclassified',
                    relevance_reasons_json TEXT NOT NULL DEFAULT '[]',
                    relevance_version TEXT,
                    retrieval_source TEXT NOT NULL DEFAULT 'search',
                    retrieval_source_id TEXT,
                    retrieval_sector_ids_json TEXT NOT NULL DEFAULT '[]',
                    author_id TEXT,
                    author_name TEXT,
                    UNIQUE(platform, content_id, comment_id)
                );
                CREATE INDEX IF NOT EXISTS idx_content_day ON content(substr(collected_at, 1, 10));
                CREATE INDEX IF NOT EXISTS idx_content_fp ON content(fingerprint);
                CREATE TABLE IF NOT EXISTS daily_scores (
                    trade_date TEXT NOT NULL, sector_id TEXT NOT NULL,
                    sector_name TEXT NOT NULL, taxonomy TEXT NOT NULL,
                    platforms_json TEXT NOT NULL, independent_authors INTEGER NOT NULL,
                    mapping_confidence REAL NOT NULL, sentiment_extreme REAL NOT NULL,
                    attention_acceleration REAL NOT NULL, consensus_crowding REAL NOT NULL,
                    market_divergence REAL NOT NULL, short_risk REAL NOT NULL,
                    swing_risk REAL NOT NULL, platform_contributions_json TEXT NOT NULL DEFAULT '{}',
                    evidence_json TEXT NOT NULL DEFAULT '[]', model_version TEXT NOT NULL,
                    rule_version TEXT NOT NULL, attention_raw REAL NOT NULL DEFAULT 0,
                    sentiment_raw REAL NOT NULL DEFAULT 0, content_count REAL NOT NULL DEFAULT 0,
                    comment_count REAL NOT NULL DEFAULT 0, engagement_raw REAL NOT NULL DEFAULT 0,
                    author_count REAL NOT NULL DEFAULT 0, market_data_complete INTEGER NOT NULL DEFAULT 1,
                    coverage_quality_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(trade_date, sector_id)
                );
                CREATE TABLE IF NOT EXISTS alert_state (
                    sector_id TEXT PRIMARY KEY, sector_name TEXT NOT NULL,
                    taxonomy TEXT NOT NULL, state TEXT NOT NULL, event_id TEXT,
                    opened_at TEXT, upgraded_at TEXT, resolved_at TEXT,
                    quiet_days INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alert_events (
                    event_key TEXT PRIMARY KEY, event_id TEXT NOT NULL,
                    sector_id TEXT NOT NULL, sector_name TEXT NOT NULL,
                    taxonomy TEXT NOT NULL, event_type TEXT NOT NULL,
                    level TEXT NOT NULL, trade_date TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    event_id TEXT PRIMARY KEY, sector_id TEXT NOT NULL,
                    signal_date TEXT NOT NULL, short_hit INTEGER, swing_hit INTEGER,
                    short_max_drawdown REAL, short_excess REAL,
                    swing_max_drawdown REAL, swing_excess REAL,
                    price_baseline_hit INTEGER, heat_baseline_hit INTEGER,
                    labeled_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_runs (
                    run_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL, coverage REAL NOT NULL,
                    search_coverage REAL, creator_coverage REAL,
                    platforms_json TEXT NOT NULL, funnel_json TEXT,
                    query_version TEXT, relevance_version TEXT,
                    creator_rule_version TEXT, semantic_prompt_version TEXT,
                    config_hash TEXT, policy_cohort_id TEXT, cohort_start_date TEXT,
                    status TEXT NOT NULL, completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS creators (
                    platform TEXT NOT NULL, creator_id TEXT NOT NULL, display_name TEXT,
                    status TEXT NOT NULL CHECK(status IN ('candidate','approved','rejected')),
                    first_discovered_at TEXT NOT NULL, last_discovered_at TEXT NOT NULL,
                    valid_content_count INTEGER NOT NULL DEFAULT 0,
                    total_content_count INTEGER NOT NULL DEFAULT 0,
                    financial_ratio REAL NOT NULL DEFAULT 0,
                    sector_ids_json TEXT NOT NULL DEFAULT '[]', evidence_json TEXT NOT NULL DEFAULT '[]',
                    reviewed_at TEXT, approved_at TEXT, last_collection_error TEXT,
                    creator_rule_version TEXT, config_hash TEXT,
                    PRIMARY KEY(platform, creator_id)
                );
                CREATE TABLE IF NOT EXISTS creator_content (
                    platform TEXT NOT NULL, creator_id TEXT NOT NULL, content_id TEXT NOT NULL,
                    relevance_score REAL NOT NULL, sector_ids_json TEXT NOT NULL DEFAULT '[]',
                    relevance_decision TEXT NOT NULL DEFAULT 'accepted',
                    relevance_reasons_json TEXT NOT NULL DEFAULT '[]',
                    query_version TEXT, relevance_version TEXT,
                    creator_rule_version TEXT, config_hash TEXT,
                    published_at TEXT, evidence_text TEXT, engagement REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(platform, creator_id, content_id)
                );
                CREATE TABLE IF NOT EXISTS creator_events (
                    event_id TEXT PRIMARY KEY, platform TEXT NOT NULL, creator_id TEXT NOT NULL,
                    action TEXT NOT NULL, previous_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    actor TEXT NOT NULL, created_at TEXT NOT NULL,
                    query_version TEXT, relevance_version TEXT,
                    creator_rule_version TEXT, config_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS collection_budgets (
                    trade_date TEXT NOT NULL, job_key TEXT NOT NULL,
                    mode TEXT NOT NULL, platform TEXT NOT NULL,
                    max_contents INTEGER NOT NULL, max_comments_per_content INTEGER NOT NULL,
                    consumed_contents INTEGER NOT NULL DEFAULT 0,
                    consumed_comments INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL, config_hash TEXT,
                    reserved_at TEXT NOT NULL, completed_at TEXT,
                    PRIMARY KEY(trade_date, job_key)
                );
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY, applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policy_day_reservations (
                    trade_date TEXT PRIMARY KEY, config_hash TEXT NOT NULL,
                    policy_cohort_id TEXT NOT NULL, cohort_start_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    reserved_at TEXT NOT NULL, completed_at TEXT
                );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(daily_scores)")}
            for name in ("attention_raw", "sentiment_raw", "content_count", "comment_count",
                         "engagement_raw", "author_count", "market_data_complete"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE daily_scores ADD COLUMN {name} REAL NOT NULL DEFAULT 0")
            content_columns = {row["name"] for row in conn.execute("PRAGMA table_info(content)")}
            migrations = {
                "relevance_score": "REAL",
                "relevance_decision": "TEXT NOT NULL DEFAULT 'unclassified'",
                "relevance_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
                "relevance_version": "TEXT",
                "retrieval_source": "TEXT NOT NULL DEFAULT 'search'",
                "retrieval_source_id": "TEXT",
                "retrieval_sector_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "author_id": "TEXT",
                "author_name": "TEXT",
                "trade_date": "TEXT",
                "semantic_text": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in migrations.items():
                if name not in content_columns:
                    conn.execute(f"ALTER TABLE content ADD COLUMN {name} {definition}")
            conn.execute(
                """UPDATE content SET trade_date=substr(collected_at,1,10)
                   WHERE trade_date IS NULL OR trade_date=''"""
            )
            conn.execute(
                """UPDATE content SET semantic_text=text
                   WHERE semantic_text IS NULL OR semantic_text=''"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_content_trade_date ON content(trade_date)")
            outcome_columns = {row["name"] for row in conn.execute("PRAGMA table_info(outcomes)")}
            for name in ("price_baseline_hit", "heat_baseline_hit"):
                if name not in outcome_columns:
                    conn.execute(f"ALTER TABLE outcomes ADD COLUMN {name} INTEGER")
            collection_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(collection_runs)")
            }
            score_columns = {row["name"] for row in conn.execute("PRAGMA table_info(daily_scores)")}
            if "coverage_quality_json" not in score_columns:
                conn.execute("ALTER TABLE daily_scores ADD COLUMN coverage_quality_json TEXT NOT NULL DEFAULT '{}'")
            legacy_collection_schema = "config_hash" not in collection_columns
            missing_search_coverage = "search_coverage" not in collection_columns
            missing_creator_coverage = "creator_coverage" not in collection_columns
            collection_migrations = {
                "search_coverage": "REAL",
                "creator_coverage": "REAL",
                "funnel_json": "TEXT",
                "query_version": "TEXT",
                "relevance_version": "TEXT",
                "creator_rule_version": "TEXT",
                "semantic_prompt_version": "TEXT",
                "config_hash": "TEXT",
                "policy_cohort_id": "TEXT",
                "cohort_start_date": "TEXT",
            }
            for name, definition in collection_migrations.items():
                if name not in collection_columns:
                    conn.execute(f"ALTER TABLE collection_runs ADD COLUMN {name} {definition}")
            if missing_search_coverage:
                conn.execute(
                    """UPDATE collection_runs
                       SET search_coverage=coverage WHERE search_coverage IS NULL"""
                )
            if missing_creator_coverage:
                conn.execute(
                    """UPDATE collection_runs
                       SET creator_coverage=coverage WHERE creator_coverage IS NULL"""
                )
            if legacy_collection_schema:
                conn.execute(
                    """UPDATE collection_runs
                       SET search_coverage=coverage, creator_coverage=coverage
                       WHERE (funnel_json IS NULL OR funnel_json='{}')
                         AND search_coverage=0 AND creator_coverage=0 AND coverage!=0"""
                )
            current_hash = current_cohort_id = current_start = None
            cohort_rows = conn.execute(
                """SELECT rowid, trade_date, config_hash, policy_cohort_id,
                          cohort_start_date
                   FROM collection_runs WHERE config_hash IS NOT NULL
                   ORDER BY trade_date, rowid"""
            ).fetchall()
            for row in cohort_rows:
                row_hash = str(row["config_hash"])
                if row_hash != current_hash:
                    current_hash = row_hash
                    current_start = str(row["trade_date"])
                    current_cohort_id = _policy_cohort_id(row_hash, current_start)
                if not row["policy_cohort_id"] or not row["cohort_start_date"]:
                    conn.execute(
                        """UPDATE collection_runs
                           SET policy_cohort_id=?, cohort_start_date=? WHERE rowid=?""",
                        (current_cohort_id, current_start, row["rowid"]),
                    )
            for row in conn.execute(
                """SELECT trade_date, config_hash, policy_cohort_id,
                          cohort_start_date, status, completed_at
                   FROM collection_runs WHERE config_hash IS NOT NULL
                   ORDER BY trade_date, rowid DESC"""
            ).fetchall():
                start = str(row["cohort_start_date"] or row["trade_date"])
                cohort_id = str(
                    row["policy_cohort_id"]
                    or _policy_cohort_id(str(row["config_hash"]), start)
                )
                conn.execute(
                    """INSERT OR IGNORE INTO policy_day_reservations
                       (trade_date, config_hash, policy_cohort_id, cohort_start_date,
                        status, reserved_at, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (row["trade_date"], row["config_hash"], cohort_id, start,
                     row["status"], row["completed_at"] or _now(), row["completed_at"]),
                )
            creator_columns = {row["name"] for row in conn.execute("PRAGMA table_info(creators)")}
            for name, definition in {
                "approved_at": "TEXT",
                "creator_rule_version": "TEXT",
                "config_hash": "TEXT",
            }.items():
                if name not in creator_columns:
                    conn.execute(f"ALTER TABLE creators ADD COLUMN {name} {definition}")
            conn.execute(
                """UPDATE creators SET approved_at=reviewed_at
                   WHERE status='approved' AND approved_at IS NULL AND reviewed_at IS NOT NULL"""
            )
            creator_content_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(creator_content)")
            }
            for name, definition in {
                "query_version": "TEXT",
                "relevance_decision": "TEXT NOT NULL DEFAULT 'accepted'",
                "relevance_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
                "relevance_version": "TEXT",
                "creator_rule_version": "TEXT",
                "config_hash": "TEXT",
                "published_at": "TEXT",
                "evidence_text": "TEXT",
                "engagement": "REAL NOT NULL DEFAULT 0",
            }.items():
                if name not in creator_content_columns:
                    conn.execute(f"ALTER TABLE creator_content ADD COLUMN {name} {definition}")
            creator_event_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(creator_events)")
            }
            for name in ("query_version", "relevance_version", "creator_rule_version", "config_hash"):
                if name not in creator_event_columns:
                    conn.execute(f"ALTER TABLE creator_events ADD COLUMN {name} TEXT")
            budget_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(collection_budgets)")
            }
            for name in ("consumed_contents", "consumed_comments"):
                if name not in budget_columns:
                    conn.execute(
                        f"ALTER TABLE collection_budgets ADD COLUMN {name} "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
            privacy_migration = "sector_evidence_privacy_v1"
            migrated = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name=?", (privacy_migration,),
            ).fetchone()
            if not migrated:
                for row in conn.execute(
                    """SELECT id, text, semantic_text, author_name,
                              relevance_reasons_json FROM content"""
                ).fetchall():
                    conn.execute(
                        """UPDATE content SET text=?, semantic_text=?, author_name=?,
                                  relevance_reasons_json=? WHERE id=?""",
                        (_redact_text(row["text"]), _redact_text(row["semantic_text"]),
                         _redact_text(row["author_name"]) or None,
                         json.dumps([
                             _redact_text(reason, 200)
                             for reason in _json_list(row["relevance_reasons_json"])
                         ], ensure_ascii=False), row["id"]),
                    )
                for row in conn.execute(
                    """SELECT rowid, evidence_text, relevance_reasons_json
                       FROM creator_content"""
                ).fetchall():
                    conn.execute(
                        """UPDATE creator_content SET evidence_text=?,
                                  relevance_reasons_json=? WHERE rowid=?""",
                        (_redact_text(row["evidence_text"], 200),
                         json.dumps([
                             _redact_text(reason, 200)
                             for reason in _json_list(row["relevance_reasons_json"])
                         ], ensure_ascii=False), row["rowid"]),
                    )
                for row in conn.execute(
                    "SELECT rowid, display_name, evidence_json FROM creators"
                ).fetchall():
                    conn.execute(
                        """UPDATE creators SET display_name=?, evidence_json=? WHERE rowid=?""",
                        (_redact_text(row["display_name"]) or None,
                         json.dumps(_sanitize_creator_evidence(row["evidence_json"]),
                                    ensure_ascii=False), row["rowid"]),
                    )
                for row in conn.execute(
                    "SELECT rowid, evidence_json FROM daily_scores"
                ).fetchall():
                    conn.execute(
                        "UPDATE daily_scores SET evidence_json=? WHERE rowid=?",
                        (json.dumps(_sanitize_score_evidence(row["evidence_json"]),
                                    ensure_ascii=False), row["rowid"]),
                    )
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                    (privacy_migration, _now()),
                )

    def ingest(
        self, items: Iterable[dict], decisions: list[RelevanceDecision] | None = None,
    ) -> dict[str, int]:
        records = list(items)
        if decisions is not None and len(decisions) != len(records):
            raise ValueError("one relevance decision is required per record")
        inserted = duplicates = 0
        with self._connect() as conn:
            for item in records:
                fp = _fingerprint(item.get("text", ""))
                try:
                    conn.execute(
                        """INSERT INTO content
                        (platform, content_id, comment_id, published_at, collected_at, trade_date,
                         text, semantic_text,
                         engagement, url, batch_id, author_hash, fingerprint, retrieval_source,
                         retrieval_source_id, retrieval_sector_ids_json, author_id, author_name)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (item["platform"], str(item["content_id"]), str(item.get("comment_id") or ""),
                         item.get("published_at"), item.get("collected_at") or _now(),
                         item.get("trade_date") or str(item.get("collected_at") or _now())[:10],
                         _redact_text(item.get("text", "")),
                         _semantic_text(item),
                         float(item.get("engagement") or 0), item.get("url"), item.get("batch_id"),
                         item.get("author_hash"), fp, item.get("retrieval_source", "search"),
                         item.get("retrieval_source_id"),
                         json.dumps(item.get("sector_ids", []), ensure_ascii=False),
                         item.get("author_id"), _redact_text(item.get("author_name")) or None),
                    )
                    inserted += 1
                    matches = conn.execute(
                        "SELECT id FROM content WHERE fingerprint=? ORDER BY id", (fp,)
                    ).fetchall()
                    if len(matches) > 1:
                        conn.executemany(
                            "UPDATE content SET repost_weight=0.35 WHERE id=?",
                            [(row["id"],) for row in matches[1:]],
                        )
                except sqlite3.IntegrityError:
                    existing = conn.execute(
                        """SELECT retrieval_sector_ids_json FROM content
                           WHERE platform=? AND content_id=? AND comment_id=?""",
                        (item["platform"], str(item["content_id"]),
                         str(item.get("comment_id") or "")),
                    ).fetchone()
                    if existing is None:
                        raise
                    duplicates += 1
                    sector_ids = sorted({
                        *json.loads(existing["retrieval_sector_ids_json"] or "[]"),
                        *(str(value) for value in item.get("sector_ids", []) if value),
                    })
                    conn.execute(
                        """UPDATE content SET retrieval_sector_ids_json=?
                           WHERE platform=? AND content_id=? AND comment_id=?""",
                        (json.dumps(sector_ids, ensure_ascii=False), item["platform"],
                         str(item["content_id"]), str(item.get("comment_id") or "")),
                    )
            for item, decision in zip(records, decisions or []):
                conn.execute(
                    """UPDATE content SET relevance_score=?, relevance_decision=?,
                       relevance_reasons_json=?, relevance_version=?
                       WHERE platform=? AND content_id=? AND comment_id=?""",
                    (decision.score, decision.decision,
                     json.dumps([_redact_text(reason, 200) for reason in decision.reasons],
                                ensure_ascii=False),
                     decision.version,
                     item["platform"], str(item["content_id"]),
                     str(item.get("comment_id") or "")),
                )
        return {"inserted": inserted, "duplicates": duplicates}

    def list_content(self, trade_date: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM content WHERE trade_date=? ORDER BY id", (trade_date,)
            ).fetchall()
        return [dict(row) for row in rows]

    def save_relevance(self, platform: str, content_id: str, comment_id: str,
                       decision: RelevanceDecision) -> None:
        """Persist a relevance decision against the canonical content key."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE content SET relevance_score=?, relevance_decision=?,
                   relevance_reasons_json=?, relevance_version=?
                   WHERE platform=? AND content_id=? AND comment_id=?""",
                (decision.score, decision.decision,
                 json.dumps([_redact_text(reason, 200) for reason in decision.reasons],
                            ensure_ascii=False),
                 decision.version,
                 platform, str(content_id), str(comment_id or "")),
            )

    def list_eligible_content(self, trade_date: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM content
                   WHERE trade_date=? AND relevance_decision='accepted'
                   ORDER BY id""", (trade_date,)
            ).fetchall()
        return [dict(row) for row in rows]

    def search_records(self, trade_date: str) -> list[dict]:
        """Return all classified search rows for creator denominator accounting."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM content
                   WHERE trade_date=? AND relevance_decision!='unclassified'
                     AND retrieval_source='search' AND comment_id=''
                   ORDER BY id""", (trade_date,),
            ).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            record["sector_ids"] = json.loads(record.pop("retrieval_sector_ids_json") or "[]")
            record["relevance_reasons"] = json.loads(record["relevance_reasons_json"] or "[]")
        return records

    def accepted_search_records(self, trade_date: str) -> list[dict]:
        """Return accepted search discoveries with their retrieval sector scope."""
        return [record for record in self.search_records(trade_date)
                if record["relevance_decision"] == "accepted"]

    def retrieval_funnel(self, trade_date: str) -> dict:
        """Compute idempotent daily funnel counts from unique persisted rows."""
        with self._connect() as conn:
            counts = conn.execute(
                """SELECT count(*) AS raw_recalled,
                          sum(CASE WHEN relevance_decision='accepted' THEN 1 ELSE 0 END)
                              AS financial_relevant
                   FROM content WHERE trade_date=? AND relevance_decision!='unclassified'""",
                (trade_date,),
            ).fetchone()
            sources = conn.execute(
                """SELECT retrieval_source, count(DISTINCT retrieval_source_id) AS n
                   FROM content WHERE trade_date=? AND relevance_decision!='unclassified'
                     AND retrieval_source_id IS NOT NULL
                   GROUP BY retrieval_source""",
                (trade_date,),
            ).fetchall()
        raw = int(counts["raw_recalled"] or 0)
        accepted = int(counts["financial_relevant"] or 0)
        by_source = {row["retrieval_source"]: int(row["n"]) for row in sources}
        return {
            "raw_recalled": raw,
            "financial_relevant": accepted,
            "filtered": raw - accepted,
            "search_sources": by_source.get("search", 0),
            "creator_sources": by_source.get("creator", 0),
        }

    def discover_creator_candidates(self, records: list[dict], settings: dict) -> dict[str, int]:
        """Account for all unique author content and create manual-review candidates."""
        minimum = int(settings.get("candidate_min_contents", 3))
        minimum_sectors = int(settings.get("candidate_min_sectors", 2))
        very_high = float(settings.get("very_high_relevance", 0.9))
        thresholds = settings.get(
            "platform_engagement_thresholds", settings.get("high_engagement_thresholds", {})
        )
        query_version = str(settings.get("query_version") or QUERY_VERSION)
        relevance_version = str(settings.get("relevance_version") or RELEVANCE_VERSION)
        creator_rule_version = str(
            settings.get("creator_rule_version") or CREATOR_RULE_VERSION
        )
        config_hash = str(settings.get("config_hash") or "") or None
        created = updated = 0
        seen_creators: dict[tuple[str, str], dict[str, Any]] = {}
        with self._connect() as conn:
            for record in records:
                platform = str(record.get("platform") or "")
                creator_id = str(record.get("author_id") or "")
                content_id = str(record.get("content_id") or "")
                score = float(record.get("relevance_score") or 0)
                decision = str(record.get("relevance_decision") or (
                    "accepted" if score >= 0.7 else "filtered_non_financial"
                ))
                if not (platform and creator_id and content_id):
                    continue
                sectors = sorted({str(value) for value in record.get("sector_ids", []) if value})
                reasons = record.get("relevance_reasons") or []
                existing_content = conn.execute(
                    """SELECT * FROM creator_content
                       WHERE platform=? AND creator_id=? AND content_id=?""",
                    (platform, creator_id, content_id),
                ).fetchone()
                new_content = existing_content is None
                if new_content:
                    conn.execute(
                        """INSERT INTO creator_content
                        (platform, creator_id, content_id, relevance_score, sector_ids_json,
                         relevance_decision, relevance_reasons_json, query_version,
                         relevance_version, creator_rule_version, config_hash, published_at,
                         evidence_text, engagement)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (platform, creator_id, content_id, score,
                         json.dumps(sectors, ensure_ascii=False), decision,
                         json.dumps([_redact_text(reason, 200) for reason in reasons],
                                    ensure_ascii=False),
                         query_version, relevance_version, creator_rule_version,
                         config_hash, record.get("published_at"),
                         _redact_text(record.get("text"), 200),
                         float(record.get("engagement") or 0)),
                    )
                entry = seen_creators.setdefault(
                    (platform, creator_id), {"records": [], "has_new_content": False},
                )
                entry["records"].append(record)
                entry["has_new_content"] = entry["has_new_content"] or new_content

            for (platform, creator_id), discovery in seen_creators.items():
                creator_records = discovery["records"]
                evidence_rows = conn.execute(
                    """SELECT rowid, * FROM creator_content
                    WHERE platform=? AND creator_id=? ORDER BY rowid""",
                    (platform, creator_id),
                ).fetchall()
                valid_rows = [row for row in evidence_rows
                              if row["relevance_decision"] == "accepted"]
                sector_ids = sorted({sector for row in valid_rows
                                     for sector in json.loads(row["sector_ids_json"])})
                valid_count = len(valid_rows)
                total_count = len(evidence_rows)
                configured_threshold = thresholds.get(
                    platform, settings.get("high_engagement_threshold", float("inf"))
                ) if isinstance(thresholds, dict) else settings.get("high_engagement_threshold", thresholds)
                if isinstance(configured_threshold, dict):
                    configured_threshold = configured_threshold.get("high_engagement_threshold", float("inf"))
                platform_threshold = float(configured_threshold)
                high_signal = any(
                    float(row["relevance_score"] or 0) >= very_high and
                    float(row["engagement"] or 0) >= platform_threshold
                    for row in valid_rows
                )
                qualifies = (
                    valid_count >= minimum
                    or high_signal
                    or len(sector_ids) >= minimum_sectors
                )
                existing = conn.execute(
                    "SELECT * FROM creators WHERE platform=? AND creator_id=?",
                    (platform, creator_id),
                ).fetchone()
                if not existing and not qualifies:
                    continue
                now = _now()
                display_name = next((record.get("author_name") for record in creator_records
                                     if record.get("author_name")), None)
                evidence = [{
                    "text": _redact_text(row["evidence_text"], 200),
                    "published_at": row["published_at"],
                    "relevance_score": float(row["relevance_score"]),
                    "reasons": json.loads(row["relevance_reasons_json"] or "[]"),
                    "query_version": row["query_version"] or query_version,
                    "relevance_version": row["relevance_version"] or relevance_version,
                    "creator_rule_version": row["creator_rule_version"] or creator_rule_version,
                    "config_hash": row["config_hash"] or config_hash,
                } for row in valid_rows[-3:]]
                if existing:
                    if not discovery["has_new_content"]:
                        continue
                    conn.execute(
                        """UPDATE creators SET display_name=COALESCE(?, display_name),
                           last_discovered_at=?, valid_content_count=?, total_content_count=?,
                           financial_ratio=?, sector_ids_json=?, evidence_json=?,
                           creator_rule_version=?, config_hash=?
                           WHERE platform=? AND creator_id=?""",
                        (_redact_text(display_name) or None, now, valid_count, total_count,
                         valid_count / total_count if total_count else 0.0,
                         json.dumps(sector_ids, ensure_ascii=False),
                         json.dumps(evidence, ensure_ascii=False), creator_rule_version,
                         config_hash, platform, creator_id),
                    )
                    updated += 1
                else:
                    conn.execute(
                        """INSERT INTO creators
                        (platform, creator_id, display_name, status, first_discovered_at,
                         last_discovered_at, valid_content_count, total_content_count,
                         financial_ratio, sector_ids_json, evidence_json,
                         creator_rule_version, config_hash)
                        VALUES (?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (platform, creator_id, _redact_text(display_name) or None, now, now,
                         valid_count, total_count,
                         valid_count / total_count if total_count else 0.0,
                         json.dumps(sector_ids, ensure_ascii=False),
                         json.dumps(evidence, ensure_ascii=False), creator_rule_version,
                         config_hash),
                    )
                    created += 1
        return {"created": created, "updated": updated}

    def list_creators(self, status: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM creators ORDER BY platform, creator_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM creators WHERE status=? ORDER BY platform, creator_id", (status,)
                ).fetchall()
        return [_decode_creator(row) for row in rows]

    def approved_creators(self, platform: str | None = None) -> list[dict]:
        creators = self.list_creators("approved")
        selected = [creator for creator in creators
                    if platform is None or creator["platform"] == platform]
        with self._connect() as conn:
            for creator in selected:
                locator = conn.execute(
                    """SELECT content_id FROM creator_content
                       WHERE platform=? AND creator_id=? AND relevance_decision='accepted'
                       ORDER BY rowid DESC LIMIT 1""",
                    (creator["platform"], creator["creator_id"]),
                ).fetchone()
                creator["crawl_locator"] = str(locator["content_id"]) if locator else None
        return selected

    def set_creator_collection_error(
        self, platform: str, creator_id: str, error: str | None,
    ) -> None:
        """Record collection health without changing the creator's review status."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE creators SET last_collection_error=?
                   WHERE platform=? AND creator_id=?""",
                (error, platform, creator_id),
            )

    def moderate_creator(self, platform: str, creator_id: str, action: str,
                         actor: str = "local-user") -> dict:
        transitions = {"approve": "approved", "reject": "rejected", "restore": "candidate"}
        if action not in transitions:
            raise ValueError(f"unknown moderation action: {action}")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM creators WHERE platform=? AND creator_id=?", (platform, creator_id)
            ).fetchone()
            if row is None:
                raise CreatorNotFoundError(platform, creator_id)
            now = _now()
            new_status = transitions[action]
            conn.execute(
                """UPDATE creators SET status=?, reviewed_at=?,
                   approved_at=CASE WHEN ?='approve' THEN COALESCE(approved_at, ?) ELSE approved_at END
                   WHERE platform=? AND creator_id=?""",
                (new_status, now, action, now, platform, creator_id),
            )
            conn.execute(
                """INSERT INTO creator_events
                (event_id, platform, creator_id, action, previous_status, new_status, actor,
                 created_at, query_version, relevance_version, creator_rule_version, config_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex, platform, creator_id, action, row["status"], new_status,
                 actor, now, QUERY_VERSION, RELEVANCE_VERSION,
                 row["creator_rule_version"] or CREATOR_RULE_VERSION, row["config_hash"]),
            )
            updated = conn.execute(
                "SELECT * FROM creators WHERE platform=? AND creator_id=?", (platform, creator_id)
            ).fetchone()
        return _decode_creator(updated)

    def creator_events(self, platform: str, creator_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM creator_events WHERE platform=? AND creator_id=?
                   ORDER BY rowid""", (platform, creator_id)
            ).fetchall()
        return [dict(row) for row in rows]

    def save_daily_score(self, score: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO daily_scores
                (trade_date, sector_id, sector_name, taxonomy, platforms_json,
                 independent_authors, mapping_confidence, sentiment_extreme,
                 attention_acceleration, consensus_crowding, market_divergence,
                 short_risk, swing_risk, platform_contributions_json, evidence_json,
                 model_version, rule_version, attention_raw, sentiment_raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (score["trade_date"], score["sector_id"], score["sector_name"], score["taxonomy"],
                 json.dumps(sorted(set(score.get("platforms", []))), ensure_ascii=False),
                 int(score.get("independent_authors", 0)), float(score.get("mapping_confidence", 0)),
                 float(score.get("sentiment_extreme", 0)), float(score.get("attention_acceleration", 0)),
                 float(score.get("consensus_crowding", 0)), float(score.get("market_divergence", 0)),
                 float(score.get("short_risk", 0)), float(score.get("swing_risk", 0)),
                 json.dumps(score.get("platform_contributions", {}), ensure_ascii=False),
                 json.dumps(_sanitize_score_evidence(score.get("evidence", [])),
                            ensure_ascii=False),
                 score.get("model_version", MODEL_VERSION), score.get("rule_version", RULE_VERSION),
                 float(score.get("attention_raw", 0)), float(score.get("sentiment_raw", 0))),
            )
            conn.execute(
                """UPDATE daily_scores SET content_count=?, comment_count=?, engagement_raw=?,
                   author_count=?, market_data_complete=?, coverage_quality_json=?
                   WHERE trade_date=? AND sector_id=?""",
                (float(score.get("content_count", 0)), float(score.get("comment_count", 0)),
                 float(score.get("engagement_raw", 0)),
                 float(score.get("author_count", score.get("independent_authors", 0))),
                 int(bool(score.get("market_data_complete", True))),
                 json.dumps(score.get("coverage_quality", score.get("coverage_groups", {})),
                            ensure_ascii=False, sort_keys=True),
                 score["trade_date"], score["sector_id"]),
            )

    def scores_for_date(self, trade_date: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_scores WHERE trade_date=? ORDER BY short_risk DESC", (trade_date,)
            ).fetchall()
        return [_decode_score(row) for row in rows]

    def scores_for_sector(self, sector_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_scores WHERE sector_id=? ORDER BY trade_date", (sector_id,)
            ).fetchall()
        return [_decode_score(row) for row in rows]

    def latest_date(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT max(trade_date) AS d FROM daily_scores").fetchone()
        return row["d"] if row else None

    def get_state(self, sector_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM alert_state WHERE sector_id=?", (sector_id,)).fetchone()
        return dict(row) if row else None

    def put_state(self, value: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO alert_state
                (sector_id, sector_name, taxonomy, state, event_id, opened_at, upgraded_at,
                 resolved_at, quiet_days, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (value["sector_id"], value["sector_name"], value["taxonomy"], value["state"],
                 value.get("event_id"), value.get("opened_at"), value.get("upgraded_at"),
                 value.get("resolved_at"), int(value.get("quiet_days", 0)), value["updated_at"]),
            )

    def record_event(self, state: dict, event_type: str, level: str, trade_date: str) -> None:
        key = f'{state["event_id"]}:{event_type}:{trade_date}'
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO alert_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, state["event_id"], state["sector_id"], state["sector_name"], state["taxonomy"],
                 event_type, level, trade_date, _now()),
            )

    def create_alert(self, sector_id: str, sector_name: str, taxonomy: str,
                     trade_date: str, level: str = "observe") -> dict:
        state = {
            "sector_id": sector_id, "sector_name": sector_name, "taxonomy": taxonomy,
            "state": level, "event_id": uuid.uuid4().hex, "opened_at": trade_date,
            "upgraded_at": trade_date if level == "warning" else None,
            "resolved_at": None, "quiet_days": 0, "updated_at": trade_date,
        }
        self.put_state(state)
        self.record_event(state, "opened", level, trade_date)
        return state

    def list_events(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM alert_events ORDER BY trade_date, created_at").fetchall()
        return [dict(row) for row in rows]

    def opened_event(self, sector_id: str, signal_date: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM alert_events WHERE sector_id=? AND trade_date=?
                   AND event_type='opened' ORDER BY created_at LIMIT 1""",
                (sector_id, signal_date),
            ).fetchone()
        return dict(row) if row else None

    def state_as_of(self, sector_id: str, trade_date: str) -> str:
        events = [event for event in self.list_events()
                  if event["sector_id"] == sector_id and event["trade_date"] <= trade_date]
        if not events:
            return "normal"
        last = events[-1]
        return {"opened": last["level"], "upgraded": "warning", "resolved": "resolved"}[last["event_type"]]

    def save_outcome(self, outcome: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO outcomes
                (event_id, sector_id, signal_date, short_hit, swing_hit,
                 short_max_drawdown, short_excess, swing_max_drawdown, swing_excess,
                 price_baseline_hit, heat_baseline_hit, labeled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (outcome["event_id"], outcome["sector_id"], outcome["signal_date"],
                 _bool_int(outcome.get("short_hit")), _bool_int(outcome.get("swing_hit")),
                 outcome.get("short_max_drawdown"), outcome.get("short_excess"),
                 outcome.get("swing_max_drawdown"), outcome.get("swing_excess"),
                 _bool_int(outcome.get("price_baseline_hit")), _bool_int(outcome.get("heat_baseline_hit")), _now()),
            )

    def record_collection(self, report: dict) -> None:
        cohort = None
        if report.get("config_hash"):
            cohort = self.reserve_policy_day(
                report["trade_date"], str(report["config_hash"]),
            )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO collection_runs
                (run_id, trade_date, coverage, search_coverage, creator_coverage,
                 platforms_json, funnel_json, query_version, relevance_version,
                 creator_rule_version, semantic_prompt_version, config_hash,
                 policy_cohort_id, cohort_start_date, status, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex, report["trade_date"], float(report["coverage"]),
                 float(report.get("search_coverage", report["coverage"])),
                 float(report.get("creator_coverage", report["coverage"])),
                 json.dumps(report.get("platforms", {}), ensure_ascii=False),
                 json.dumps(report.get("funnel"), ensure_ascii=False),
                 report.get("query_version"), report.get("relevance_version"),
                 report.get("creator_rule_version"), report.get("semantic_prompt_version"),
                 report.get("config_hash"),
                 report.get("policy_cohort_id") or (cohort or {}).get("policy_cohort_id"),
                 report.get("cohort_start_date") or (cohort or {}).get("cohort_start_date"),
                 "ok" if report["coverage"] == 1 else "degraded", _now()),
            )
            if report.get("config_hash"):
                conn.execute(
                    """UPDATE policy_day_reservations SET status=?, completed_at=?
                       WHERE trade_date=? AND config_hash=?""",
                    ("ok" if report["coverage"] == 1 else "degraded", _now(),
                     report["trade_date"], report["config_hash"]),
                )

    def reserve_policy_day(self, trade_date: str, config_hash: str) -> dict[str, str]:
        """Bind a date to one policy before budgets, collection or ingest can mutate state."""
        cohort = self.resolve_policy_cohort(trade_date, config_hash)
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO policy_day_reservations
                   (trade_date, config_hash, policy_cohort_id, cohort_start_date,
                    status, reserved_at)
                   VALUES (?, ?, ?, ?, 'running', ?)""",
                (trade_date, config_hash, cohort["policy_cohort_id"],
                 cohort["cohort_start_date"], _now()),
            )
            row = conn.execute(
                """SELECT config_hash, policy_cohort_id, cohort_start_date
                   FROM policy_day_reservations WHERE trade_date=?""",
                (trade_date,),
            ).fetchone()
        if row is None or row["config_hash"] != config_hash:
            raise ValueError(
                "sector sentiment trade date is reserved by a conflicting policy"
            )
        return {
            "policy_cohort_id": str(row["policy_cohort_id"]),
            "cohort_start_date": str(row["cohort_start_date"]),
        }

    def resolve_policy_cohort(self, trade_date: str, config_hash: str) -> dict[str, str]:
        """Resolve the audited cohort while enforcing 60 trading-date freezes."""
        target = date.fromisoformat(trade_date)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT trade_date, config_hash, policy_cohort_id, cohort_start_date
                   FROM (
                       SELECT trade_date, config_hash, policy_cohort_id, cohort_start_date,
                              0 AS source_priority, rowid AS source_sequence
                       FROM collection_runs WHERE config_hash IS NOT NULL
                       UNION ALL
                       SELECT trade_date, config_hash, policy_cohort_id, cohort_start_date,
                              1 AS source_priority, rowid AS source_sequence
                       FROM policy_day_reservations
                   )
                   ORDER BY trade_date, source_priority, source_sequence"""
            ).fetchall()
        by_date: dict[date, dict[str, str]] = {}
        for row in rows:
            try:
                run_date = date.fromisoformat(str(row["trade_date"]))
            except ValueError:
                continue
            row_hash = str(row["config_hash"])
            start = str(row["cohort_start_date"] or row["trade_date"])
            by_date[run_date] = {
                "config_hash": row_hash,
                "policy_cohort_id": str(
                    row["policy_cohort_id"] or _policy_cohort_id(row_hash, start)
                ),
                "cohort_start_date": start,
            }
        if not by_date:
            return {
                "policy_cohort_id": _policy_cohort_id(config_hash, trade_date),
                "cohort_start_date": trade_date,
            }

        ordered_dates = sorted(by_date)
        if target <= ordered_dates[-1]:
            required_date = next(run_date for run_date in ordered_dates if run_date >= target)
            required = by_date[required_date]
            if required["config_hash"] != config_hash:
                raise ValueError(
                    "sector sentiment retrieval policy is frozen for this historical cohort"
                )
            return {
                "policy_cohort_id": required["policy_cohort_id"],
                "cohort_start_date": required["cohort_start_date"],
            }

        current = by_date[ordered_dates[-1]]
        current_hash = current["config_hash"]
        if config_hash == current_hash:
            return {
                "policy_cohort_id": current["policy_cohort_id"],
                "cohort_start_date": current["cohort_start_date"],
            }
        cohort_size = sum(
            value["policy_cohort_id"] == current["policy_cohort_id"]
            for value in by_date.values()
        )
        if cohort_size < 60:
            raise ValueError(
                "sector sentiment retrieval policy is frozen until 60 trading dates complete"
            )
        return {
            "policy_cohort_id": _policy_cohort_id(config_hash, trade_date),
            "cohort_start_date": trade_date,
        }

    def validate_frozen_policy(self, trade_date: str, config_hash: str) -> None:
        self.resolve_policy_cohort(trade_date, config_hash)

    def collection_summary(self, trade_date: str | None = None) -> dict:
        with self._connect() as conn:
            if trade_date:
                row = conn.execute(
                    """SELECT * FROM collection_runs WHERE trade_date=?
                       ORDER BY completed_at DESC, rowid DESC LIMIT 1""",
                    (trade_date,),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM collection_runs
                       ORDER BY trade_date DESC, completed_at DESC, rowid DESC LIMIT 1"""
                ).fetchone()
            if row and row["policy_cohort_id"]:
                days = conn.execute(
                    """SELECT count(DISTINCT trade_date) AS n FROM collection_runs
                       WHERE policy_cohort_id=?""",
                    (row["policy_cohort_id"],),
                ).fetchone()["n"]
            else:
                days = conn.execute(
                    "SELECT count(DISTINCT trade_date) AS n FROM collection_runs"
                ).fetchone()["n"]
        if not row:
            return {"trading_days": days, "coverage": 0.0, "status": "no_data", "trade_date": trade_date}
        value = dict(row)
        value["trading_days"] = days
        value["platforms"] = json.loads(value.pop("platforms_json"))
        funnel = json.loads(value.pop("funnel_json") or "null")
        value["funnel"] = funnel or None
        return value

    def eastmoney_shadow_progress(self) -> dict[str, int]:
        """Count distinct Eastmoney shadow attempts and quality-qualified dates."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trade_date, platforms_json FROM collection_runs ORDER BY trade_date"
            ).fetchall()
        attempts: set[str] = set()
        qualified: set[str] = set()
        for row in rows:
            try:
                eastmoney = json.loads(row["platforms_json"] or "{}").get("eastmoney")
            except (json.JSONDecodeError, AttributeError):
                continue
            if not isinstance(eastmoney, dict) or eastmoney.get("phase", "shadow") != "shadow":
                continue
            attempts.add(str(row["trade_date"]))
            if eastmoney.get("shadow_qualified") is True:
                qualified.add(str(row["trade_date"]))
        return {"attempt_days": len(attempts), "qualified_days": len(qualified)}

    def latest_successful_eastmoney_date(
        self, on_or_before: str | None = None,
    ) -> str | None:
        """Return the newest promoted Eastmoney date with a qualified saved score."""
        params: tuple[str, ...] = ()
        where = ""
        if on_or_before is not None:
            where = "WHERE trade_date<=?"
            params = (on_or_before,)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT trade_date, platforms_json FROM collection_runs
                    {where}
                    ORDER BY trade_date DESC, completed_at DESC, rowid DESC""",
                params,
            ).fetchall()
        for row in rows:
            trade_date = str(row["trade_date"])
            try:
                eastmoney = json.loads(row["platforms_json"] or "{}").get("eastmoney")
            except (json.JSONDecodeError, AttributeError):
                continue
            if not isinstance(eastmoney, dict):
                continue
            status = str(eastmoney.get("current_status") or eastmoney.get("status") or "")
            if eastmoney.get("phase") != "promoted" or status not in {"ok", "empty_valid"}:
                continue
            if any(_coverage_ok(score) for score in self.scores_for_date(trade_date)):
                return trade_date
        return None


    def reserve_collection_budget(
        self, trade_date: str, job: RetrievalJob, limits: dict,
    ) -> bool:
        """Atomically reserve the one allowed invocation for a date/job pair."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO collection_budgets
                   (trade_date, job_key, mode, platform, max_contents,
                    max_comments_per_content, status, config_hash, reserved_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                (trade_date, job.budget_key, job.mode, job.platform,
                 max(0, int(limits.get("max_contents", 0))),
                 max(0, int(limits.get("max_comments", 0))),
                 job.config_hash, _now()),
            )
        return cursor.rowcount == 1

    def complete_collection_budget(
        self, trade_date: str, job: RetrievalJob, status: str, *,
        consumed_contents: int = 0, consumed_comments: int = 0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE collection_budgets SET status=?, completed_at=?,
                          consumed_contents=?, consumed_comments=?
                   WHERE trade_date=? AND job_key=?""",
                (status, _now(), max(0, int(consumed_contents)),
                 max(0, int(consumed_comments)), trade_date, job.budget_key),
            )

    def set_collection_budget_status(
        self, trade_date: str, job_key: str, status: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE collection_budgets SET status=?, completed_at=?
                   WHERE trade_date=? AND job_key=?""",
                (status, _now(), trade_date, job_key),
            )

    def collection_budget(self, trade_date: str, job: RetrievalJob) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM collection_budgets
                   WHERE trade_date=? AND job_key=?""",
                (trade_date, job.budget_key),
            ).fetchone()
        return dict(row) if row else None

    def collection_budgets(self, trade_date: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT trade_date, job_key, mode, platform, max_contents,
                          max_comments_per_content, status, config_hash
                   FROM collection_budgets WHERE trade_date=? ORDER BY rowid""",
                (trade_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def collection_dates(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT trade_date FROM collection_runs ORDER BY trade_date").fetchall()
        return [row["trade_date"] for row in rows]

    def validation_rows(self) -> list[dict]:
        current = self.collection_summary()
        cohort_start = current.get("cohort_start_date")
        with self._connect() as conn:
            if cohort_start:
                rows = conn.execute(
                    """SELECT * FROM outcomes WHERE signal_date>=?
                       ORDER BY signal_date""",
                    (cohort_start,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM outcomes ORDER BY signal_date").fetchall()
        return [dict(row) for row in rows]


def _sector_sentiment_meta(
    store: SentimentStore, date: str | None, *, score_date: str | None = None,
) -> dict:
    resolved = date or store.latest_date()
    score_resolved = score_date or resolved
    scores = store.scores_for_date(score_resolved) if score_resolved else []
    model_version = scores[0]["model_version"] if scores else MODEL_VERSION
    return {
        "as_of": resolved, "score_as_of": score_resolved,
        "model_version": model_version, "rule_version": RULE_VERSION,
        "prompt_version": PROMPT_VERSION, "dictionary_version": DICTIONARY_VERSION,
        "mapping_version": MAPPING_VERSION,
    }


def _sector_sentiment_display_dates(
    store: SentimentStore, requested_date: str | None,
) -> tuple[str | None, str | None, bool, bool]:
    """Resolve the current audit date separately from the score safe to display."""
    if requested_date is not None:
        attempted = requested_date
    else:
        latest_run = store.collection_summary()
        attempted = (latest_run.get("trade_date")
                     if latest_run.get("status") != "no_data" else store.latest_date())
    score_date = attempted or store.latest_date()
    if not attempted:
        return attempted, score_date, False, False
    eastmoney = (store.collection_summary(attempted).get("platforms") or {}).get("eastmoney")
    promoted_failure = isinstance(eastmoney, dict) and eastmoney.get("phase") == "promoted" and str(
        eastmoney.get("current_status") or eastmoney.get("status") or "failed"
    ) not in {"ok", "empty_valid"}
    if promoted_failure:
        previous = store.latest_successful_eastmoney_date(attempted)
        if previous:
            return attempted, previous, True, True
    return attempted, score_date, False, bool(promoted_failure)


def sector_sentiment_quality_meta(
    store: SentimentStore, date: str | None, *, expected_platforms: int,
    score_date: str | None = None, stale: bool | None = None,
    force_degraded: bool = False,
) -> dict:
    """Project collection and score metadata without exposing store internals."""
    resolved = date or store.latest_date()
    score_resolved = score_date or resolved
    sectors = store.scores_for_date(score_resolved) if score_resolved else []
    run = store.collection_summary(resolved if date else None)
    if run["status"] == "no_data":
        present = {platform for sector in sectors for platform in sector["platforms"]}
        coverage = min(1.0, len(present) / max(1, expected_platforms))
    else:
        coverage = float(run["coverage"])
    quality = "ok" if sectors and coverage >= 1 else "no_data" if not sectors else "degraded"
    if stale is None:
        stale = bool(resolved and score_resolved and run.get("trade_date")
                     and score_resolved < run["trade_date"])
    if force_degraded or stale:
        quality = "degraded"
    return {**_sector_sentiment_meta(store, resolved, score_date=score_resolved),
            "coverage": coverage, "data_quality": quality, "stale": bool(stale)}


def sector_sentiment_eastmoney_telemetry(store: SentimentStore, date: str | None) -> dict | None:
    """Return the public Eastmoney operational projection, excluding raw telemetry."""
    raw = (store.collection_summary(date).get("platforms") or {}).get("eastmoney")
    if not isinstance(raw, dict):
        return None
    current = str(raw.get("current_status") or raw.get("status") or "failed")
    display = str(raw.get("display_status") or current)
    last_success = raw.get("last_success") if isinstance(raw.get("last_success"), dict) else {}
    shown = last_success if display == "stale" and last_success else raw
    progress = store.eastmoney_shadow_progress()
    public = {
        "current_status": current, "display_status": display,
        "posts": int(shown.get("posts") or 0),
        "first_level_comments": int(shown.get("comments") or 0),
        "independent_authors": int(shown.get("independent_authors") or 0),
        "sector_forum_records": int(shown.get("sector_forum_records") or 0),
        "constituent_forum_records": int(shown.get("constituent_forum_records") or 0),
        "request_success_rate": float(shown.get("request_success_rate") or 0),
        "parse_success_rate": float(shown.get("parse_success_rate") or 0),
        "quota_exhausted": bool(raw.get("quota_exhausted")),
        "circuit_open": bool(raw.get("circuit_open")),
        "schema_changed": current == "schema_changed", "blocked": current == "blocked",
        "stale": display == "stale" or bool(raw.get("stale")),
        "current_attempt_at": raw.get("as_of"),
        "latest_success_at": (raw.get("last_success_at") or last_success.get("as_of")
                              or (raw.get("as_of") if current in {"ok", "empty_valid"} else None)),
        "phase": str(raw.get("phase") or "shadow"),
        "shadow_attempt_days": progress["attempt_days"],
        "shadow_qualified_days": progress["qualified_days"],
        "shadow_days": min(progress["qualified_days"], EASTMONEY_PROMOTION_QUALIFIED_DAYS),
        "shadow_target_days": EASTMONEY_PROMOTION_QUALIFIED_DAYS,
    }
    promotion_audit = raw.get("promotion_audit")
    if isinstance(promotion_audit, dict):
        public["override_used"] = bool(promotion_audit.get("override_used"))
    return public


def _score_is_qualified_for_display(score: dict) -> bool:
    groups = score.get("coverage_quality") or score.get("coverage_groups") or {}
    return _coverage_ok(score) if "qualified" in groups else _detail_ok_for_display(score)


def _detail_ok_for_display(score: dict) -> bool:
    return (len(score["platforms"]) >= MIN_PLATFORMS
            and score["independent_authors"] >= MIN_AUTHORS
            and score["mapping_confidence"] >= MIN_MAPPING_CONFIDENCE)


def sector_sentiment_overview(
    store: SentimentStore, requested_date: str | None, *, expected_platforms: int,
) -> dict:
    """Build the read-only overview while keeping audit and score dates distinct."""
    resolved, score_date, stale, promoted_failure = _sector_sentiment_display_dates(store, requested_date)
    sectors = store.scores_for_date(score_date) if score_date else []
    for sector in sectors:
        sector["state"] = ("insufficient_data" if stale or promoted_failure
                           or not _score_is_qualified_for_display(sector)
                           else store.state_as_of(sector["sector_id"], score_date))
    return {
        **sector_sentiment_quality_meta(
            store, resolved, expected_platforms=expected_platforms, score_date=score_date,
            stale=stale, force_degraded=promoted_failure,
        ),
        "sectors": sectors,
        "changes": [event for event in store.list_events() if event["trade_date"] == resolved],
        "shadow_mode": True, "retrieval_funnel": store.collection_summary(resolved).get("funnel"),
        "eastmoney": sector_sentiment_eastmoney_telemetry(store, resolved),
    }


def sector_sentiment_detail(
    store: SentimentStore, sector_id: str, requested_date: str | None, *, expected_platforms: int,
) -> dict | None:
    """Build one sector's public view, or return ``None`` when it is absent."""
    resolved, score_date, stale, promoted_failure = _sector_sentiment_display_dates(store, requested_date)
    history = store.scores_for_sector(sector_id)
    if score_date:
        history = [row for row in history if row["trade_date"] <= score_date]
    if not history:
        return None
    latest = history[-1]
    latest["state"] = ("insufficient_data" if stale or promoted_failure
                       or not _score_is_qualified_for_display(latest)
                       else store.state_as_of(sector_id, latest["trade_date"]))
    meta = sector_sentiment_quality_meta(
        store, resolved, expected_platforms=expected_platforms,
        score_date=latest["trade_date"], stale=stale, force_degraded=promoted_failure,
    )
    quality = "ok" if meta["data_quality"] == "ok" and _detail_ok_for_display(latest) else "degraded"
    return {**meta, "data_quality": quality, "sector": latest,
            "state": store.get_state(sector_id), "history": history[-60:],
            "retrieval_funnel": store.collection_summary(resolved).get("funnel"),
            "eastmoney": sector_sentiment_eastmoney_telemetry(store, resolved)}


def _decode_score(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["platforms"] = json.loads(value.pop("platforms_json"))
    value["platform_contributions"] = json.loads(value.pop("platform_contributions_json"))
    value["evidence"] = _sanitize_score_evidence(value.pop("evidence_json"))
    value["coverage_quality"] = json.loads(value.pop("coverage_quality_json", "{}") or "{}")
    value["coverage_groups"] = {
        key: value["coverage_quality"].get(key, False)
        for key in ("short_video", "finance_community", "qualified")
    }
    return value


def _decode_creator(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["sector_ids"] = json.loads(value.pop("sector_ids_json"))
    value["evidence"] = _sanitize_creator_evidence(value.pop("evidence_json"))
    return value


def _bool_int(value: bool | None) -> int | None:
    return None if value is None else int(bool(value))


def _coverage_ok(score: dict) -> bool:
    groups = score.get("coverage_quality") or score.get("coverage_groups") or {}
    return (groups.get("qualified") is True
            and len(score["platforms"]) >= MIN_PLATFORMS
            and score["independent_authors"] >= MIN_AUTHORS
            and score["mapping_confidence"] >= MIN_MAPPING_CONFIDENCE)


def build_daily_score(*, trade_date: str, sector_id: str, sector_name: str,
                      taxonomy: str, evidence: list[dict], history: list[dict],
                      market: dict, coverage_quality: dict | None = None) -> dict:
    """Aggregate each platform first, then combine platforms equally."""
    grouped: dict[str, list[dict]] = {}
    for row in evidence:
        grouped.setdefault(row["platform"], []).append(row)
    contributions = {}
    for platform, rows in grouped.items():
        weights = [max(0.05, float(row.get("confidence", 0))) * float(row.get("repost_weight", 1.0))
                   for row in rows]
        original_weights = list(weights)
        total = sum(weights) or 1.0
        stock_shares = {}
        if platform == "eastmoney":
            by_stock: dict[str, list[int]] = {}
            for index, row in enumerate(rows):
                if row.get("source_type") == "constituent_forum" and row.get("stock_code"):
                    by_stock.setdefault(str(row["stock_code"]), []).append(index)
            for stock_code, indexes in by_stock.items():
                stock_total = sum(weights[index] for index in indexes)
                allowed = min(stock_total, total * 0.30)
                if stock_total > 0 and allowed < stock_total:
                    scale = allowed / stock_total
                    for index in indexes:
                        weights[index] *= scale
                stock_shares[stock_code] = allowed / total
        multipliers = [weight / original if original else 0.0
                       for weight, original in zip(weights, original_weights)]
        author_weights: dict[str, float] = {}
        fingerprint_weights: dict[str, float] = {}
        for row, multiplier in zip(rows, multipliers):
            if row.get("author_hash"):
                author = str(row["author_hash"])
                author_weights[author] = max(author_weights.get(author, 0.0), multiplier)
            fingerprint = _fingerprint(str(row.get("text", "")))
            fingerprint_weights[fingerprint] = max(
                fingerprint_weights.get(fingerprint, 0.0), multiplier
            )
        effective_records = sum(multipliers) or 1.0
        suppressed_mass = max(0.0, float(len(rows)) - effective_records)
        net = sum(float(row.get("stance", 0)) * weight for row, weight in zip(rows, weights)) / total
        contributions[platform] = {
            "records": len(rows),
            "posts": sum(multiplier for row, multiplier in zip(rows, multipliers)
                         if not row.get("comment_id")),
            "comments": sum(multiplier for row, multiplier in zip(rows, multipliers)
                            if row.get("comment_id")),
            "net_sentiment": net,
            "fomo": sum(float(row.get("fomo", 0)) * weight for row, weight in zip(rows, weights)) / total,
            "panic": sum(float(row.get("panic", 0)) * weight for row, weight in zip(rows, weights)) / total,
            "constituent_stock_shares": stock_shares,
            "engagement_raw": sum((1 + float(row.get("engagement", 0))) ** 0.5
                                  * multiplier for row, multiplier in zip(rows, multipliers)),
            "authors": sum(author_weights.values()),
            # Capped-away constituent mass is neutral/non-crowding evidence. Without this,
            # 100 identical comments capped to 30% still look 100% identical and inflate crowding.
            "diversity": ((sum(fingerprint_weights.values()) + suppressed_mass)
                          / max(1.0, float(len(rows)))),
        }
    platform_values = list(contributions.values())
    coverage_groups = {
        "short_video": any(platform in grouped for platform in ("bili", "dy")),
        "finance_community": "eastmoney" in grouped,
    }
    coverage_groups["qualified"] = all(coverage_groups.values())
    coverage_quality = dict(coverage_quality or coverage_groups)
    net_sentiment = (sum(row["net_sentiment"] for row in platform_values) / len(platform_values)
                     if platform_values else 0.0)
    fomo = sum(row["fomo"] for row in platform_values) / len(platform_values) if platform_values else 0.0
    diversity = (sum(row["diversity"] for row in platform_values) / len(platform_values)
                 if platform_values else 1.0)
    bullish_consensus = min(1.0, 0.7 * abs(net_sentiment) + 0.3 * (1 - diversity))
    content_count = (sum(row["posts"] for row in platform_values) / len(platform_values)
                     if platform_values else 0.0)
    comment_count = (sum(row["comments"] for row in platform_values) / len(platform_values)
                     if platform_values else 0.0)
    engagement_raw = (sum(row["engagement_raw"] for row in platform_values) / len(platform_values)
                      if platform_values else 0.0)
    author_count = (sum(row["authors"] for row in platform_values) / len(platform_values)
                    if platform_values else 0.0)
    recent5 = history[-5:]
    recent20 = history[-20:]
    def relative_growth(key: str, current: float) -> float:
        avg5 = sum(float(row.get(key, 0)) for row in recent5) / len(recent5) if recent5 else current
        avg20 = sum(float(row.get(key, 0)) for row in recent20) / len(recent20) if recent20 else avg5
        return current / max(1e-6, (avg5 + avg20) / 2)
    attention_raw = sum([
        relative_growth("content_count", content_count),
        relative_growth("comment_count", comment_count),
        relative_growth("engagement_raw", engagement_raw),
        relative_growth("author_count", author_count),
    ]) / 4
    prior_attention = [float(row.get("attention_raw", 0)) for row in history[-20:]]
    attention_acceleration = _percentile(attention_raw, prior_attention)
    sentiment_extreme = _percentile(max(0.0, net_sentiment) * 0.7 + fomo * 0.3,
                                    [float(row.get("sentiment_raw", 0)) for row in history[-60:]])
    market_keys = ("return", "volume_change", "breadth", "fund_flow")
    market_complete = all(market.get(key) is not None for key in market_keys)
    confirmations = []
    if market.get("return") is not None:
        confirmations.append(max(0.0, -float(market["return"])) / 3)
    if market.get("volume_change") is not None:
        confirmations.append(max(0.0, -float(market["volume_change"])))
    if market.get("breadth") is not None:
        confirmations.append(max(0.0, 0.5 - float(market["breadth"])) * 2)
    if market.get("fund_flow") is not None:
        confirmations.append(max(0.0, -float(market["fund_flow"])) / 3)
    negative_confirmation = sum(confirmations) / len(confirmations) if confirmations else 0.0
    divergence = min(1.0, negative_confirmation + max(0, sentiment_extreme - 0.8))
    mapping = (sum(float(row.get("mapping_confidence", 0)) for row in evidence) / len(evidence)
               if evidence else 0.0)
    authors = {row.get("author_hash") for row in evidence if row.get("author_hash")}
    short_risk = round(100 * (0.30 * sentiment_extreme + 0.25 * attention_acceleration
                              + 0.20 * bullish_consensus + 0.25 * divergence), 1)
    swing_risk = round(100 * (0.20 * sentiment_extreme + 0.15 * attention_acceleration
                              + 0.25 * bullish_consensus + 0.40 * divergence), 1)
    return {
        "trade_date": trade_date, "sector_id": sector_id, "sector_name": sector_name,
        "taxonomy": taxonomy, "platforms": sorted(grouped), "independent_authors": len(authors),
        "mapping_confidence": mapping, "net_sentiment": net_sentiment,
        "sentiment_extreme": sentiment_extreme, "attention_acceleration": attention_acceleration,
        "consensus_crowding": bullish_consensus, "market_divergence": divergence,
        "short_risk": short_risk, "swing_risk": swing_risk,
        "platform_contributions": contributions,
        "coverage_groups": coverage_groups,
        "coverage_quality": coverage_quality,
        "evidence": [{"platform": row["platform"], "text": _redact_text(row.get("text", ""), 160),
                      "stance": row.get("stance", 0),
                      **({"url": row["url"]} if row.get("platform") == "eastmoney"
                         and str(row.get("url") or "").startswith(
                             "https://guba.eastmoney.com/"
                         ) else {})} for row in evidence[:8]],
        "attention_raw": attention_raw,
        "sentiment_raw": max(0.0, net_sentiment) * 0.7 + fomo * 0.3,
        "content_count": content_count, "comment_count": comment_count, "engagement_raw": engagement_raw,
        "author_count": author_count,
        "market_data_complete": market_complete,
        "model_version": MODEL_VERSION, "rule_version": RULE_VERSION,
    }


def _percentile(value: float, history: list[float]) -> float:
    if not history:
        return min(1.0, max(0.0, value))
    return sum(item <= value for item in history) / len(history)


def advance_alerts(store: SentimentStore, trade_date: str) -> list[dict]:
    results = []
    for score in store.scores_for_date(trade_date):
        previous = store.get_state(score["sector_id"])
        if not _coverage_ok(score):
            results.append({**score, "state": "insufficient_data", "changed": False})
            continue
        if previous and previous.get("updated_at", "") >= trade_date:
            results.append({
                **score, "state": previous["state"], "changed": False,
                "event_id": previous.get("event_id"),
            })
            continue
        hot = score["sentiment_extreme"] >= 0.90 or score["attention_acceleration"] >= 0.90
        observed_days = 0
        if previous and previous.get("opened_at"):
            eligible = [row["trade_date"] for row in store.scores_for_sector(score["sector_id"])
                        if previous["opened_at"] <= row["trade_date"] <= trade_date
                        and _coverage_ok(row)]
            collection_dates = [day for day in store.collection_dates()
                                if previous["opened_at"] <= day <= trade_date]
            observed_days = len(eligible) if not collection_dates else (
                2 if len(collection_dates) >= 2 and eligible[-2:] == collection_dates[-2:] else 1
            )
        divergent = bool(score.get("market_data_complete", True)) and score["market_divergence"] >= 0.60 and (
            score["consensus_crowding"] >= 0.90
            or observed_days >= 2
        )
        cool = score["sentiment_extreme"] < 0.70 and score["attention_acceleration"] < 0.70
        changed = False

        if not previous or previous["state"] == "resolved":
            if hot:
                previous = store.create_alert(score["sector_id"], score["sector_name"],
                                              score["taxonomy"], trade_date, "observe")
                changed = True
            else:
                results.append({**score, "state": "resolved" if previous else "normal", "changed": False})
                continue
        elif previous["state"] == "observe" and divergent:
            previous.update(state="warning", upgraded_at=trade_date, quiet_days=0, updated_at=trade_date)
            store.put_state(previous)
            store.record_event(previous, "upgraded", "warning", trade_date)
            changed = True
        elif previous["state"] in ("observe", "warning"):
            previous["quiet_days"] = previous["quiet_days"] + 1 if cool else 0
            previous["updated_at"] = trade_date
            price_reconfirmed = cool and score["market_divergence"] < 0.20
            if price_reconfirmed or previous["quiet_days"] >= 3:
                previous.update(state="resolved", resolved_at=trade_date)
                store.put_state(previous)
                store.record_event(previous, "resolved", "resolved", trade_date)
                changed = True
            else:
                store.put_state(previous)
        results.append({**score, "state": previous["state"], "changed": changed,
                        "event_id": previous.get("event_id")})
    return results


def label_event(store: SentimentStore, sector_id: str, signal_date: str,
                prices: list[dict]) -> dict:
    event = store.opened_event(sector_id, signal_date)
    if not event:
        raise ValueError("alert event not found")
    future = sorted((row for row in prices if row["trade_date"] > signal_date),
                    key=lambda row: row["trade_date"])

    def horizon(n: int) -> tuple[float | None, float | None]:
        rows = future[:n]
        if not rows:
            return None, None
        cumulative = 0.0
        minimum = 0.0
        benchmark = 0.0
        for row in rows:
            cumulative += float(row["sector_return"])
            benchmark += float(row["benchmark_return"])
            minimum = min(minimum, cumulative)
        return minimum, cumulative - benchmark

    short_dd, short_excess = horizon(3)
    swing_dd, swing_excess = horizon(10)
    signal_row = next((row for row in prices if row["trade_date"] == signal_date), {})
    result = {
        "event_id": event["event_id"], "sector_id": sector_id, "signal_date": signal_date,
        "short_max_drawdown": short_dd, "short_excess": short_excess,
        "swing_max_drawdown": swing_dd, "swing_excess": swing_excess,
        "short_hit": None if len(future) < 3 else short_dd <= -3 or short_excess <= -2,
        "swing_hit": None if len(future) < 10 else swing_dd <= -6 or swing_excess <= -4,
        "price_baseline_hit": signal_row.get("price_baseline_hit"),
        "heat_baseline_hit": signal_row.get("heat_baseline_hit"),
    }
    store.save_outcome(result)
    return result


def evaluate_layer(rows: list[dict], hit_key: str, *, samples: int = 1000) -> dict:
    """Compare event precision with both baselines using date-cluster bootstrap."""
    valid = [row for row in rows if row.get(hit_key) is not None
             and row.get("price_baseline_hit") is not None
             and row.get("heat_baseline_hit") is not None]
    if not valid:
        return {"n": 0, "precision": None, "best_baseline": None, "uplift": None,
                "ci_low": None, "ci_high": None, "verdict": "PENDING"}
    dates: dict[str, list[dict]] = {}
    for row in valid:
        dates.setdefault(row["signal_date"], []).append(row)

    def diff(sample_rows: list[dict]) -> tuple[float, float, float]:
        precision = sum(int(row[hit_key]) for row in sample_rows) / len(sample_rows)
        price = sum(int(row.get("price_baseline_hit") or 0) for row in sample_rows) / len(sample_rows)
        heat = sum(int(row.get("heat_baseline_hit") or 0) for row in sample_rows) / len(sample_rows)
        baseline = max(price, heat)
        return precision, baseline, precision - baseline

    precision, baseline, uplift = diff(valid)
    rng = random.Random(20260810)
    keys = sorted(dates)
    boot = []
    for _ in range(samples):
        sample = [row for _key in keys for row in dates[rng.choice(keys)]]
        boot.append(diff(sample)[2])
    boot.sort()
    low = boot[int(samples * 0.025)]
    high = boot[min(samples - 1, int(samples * 0.975))]
    verdict = "GO" if len(valid) >= 30 and uplift >= 0.10 and low > 0 else "NO-GO" if len(valid) >= 30 else "PENDING"
    return {"n": len(valid), "precision": precision, "best_baseline": baseline,
            "uplift": uplift, "ci_low": low, "ci_high": high, "verdict": verdict}


def default_mediacrawler_runner(
    root: Path, commit: str | None = None, creator_id_argument: str = "--creator_id",
) -> Callable:
    """Return a subprocess runner for a separately installed MediaCrawler checkout."""
    root = root.expanduser().resolve()

    def run(job: RetrievalJob, destination: Path, timeout: int, limits: dict) -> None:
        if not (root / "main.py").exists():
            raise FileNotFoundError(f"MediaCrawler not found: {root}")
        if job.mode == "creator" and not job.cutoff:
            raise ValueError("creator retrieval requires an immutable approval cutoff")
        if job.mode == "creator" and not job.crawl_locator:
            raise ValueError("creator retrieval requires a verified content locator")
        if commit:
            actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                    text=True, check=True, timeout=10).stdout.strip()
            if actual != commit:
                raise RuntimeError(f"MediaCrawler commit mismatch: expected {commit}, got {actual}")
            dirty = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=root, capture_output=True, text=True, check=True, timeout=10,
            ).stdout.strip()
            if dirty:
                raise RuntimeError("MediaCrawler working tree is dirty; pinned code is not immutable")
        max_contents = max(0, int(limits.get("max_contents", 20)))
        max_comments = max(0, int(limits.get("max_comments", 50)))
        stage = destination.parent / f".{destination.name}.mediacrawler"
        stage.mkdir(parents=True, exist_ok=False)
        entrypoint = (
            Path(__file__).with_name("mediacrawler_bounded.py")
            if job.mode == "creator" else Path("main.py")
        )
        cmd = [str(root / ".venv" / "bin" / "python"), str(entrypoint),
               "--platform", job.platform,
               "--type", job.mode,
               "--crawler_max_notes_count", str(max_contents),
               "--max_comments_count_singlenotes", str(max_comments),
               "--get_comment", "true" if max_comments else "false",
               "--get_sub_comment", "false",
               "--max_concurrency_num", "1",
               "--save_data_option", "jsonl",
               "--save_data_path", str(stage)]
        if job.mode == "search":
            cmd.extend(["--keywords", job.value])
        else:
            cmd.extend([creator_id_argument, str(job.crawl_locator)])
        process_env = None
        if job.mode == "creator":
            process_env = os.environ.copy()
            process_env.update({
                "APEX_CREATOR_CONTENT_ID": str(job.crawl_locator),
                "APEX_APPROVED_CREATOR_HASH": job.value,
            })
        subprocess.run(cmd, cwd=root, check=True, timeout=timeout, env=process_env)
        candidates = list(stage.rglob("*.jsonl"))
        if not candidates:
            raise RuntimeError("collector finished without new JSONL output")
        normalized_rows = []
        for source in sorted(candidates):
            for line in source.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict):
                        continue
                    normalized = normalize_external_record(
                        raw, job.platform, trade_date=job.trade_date,
                    )
                except (json.JSONDecodeError, TypeError, ValueError, OverflowError):
                    continue
                if normalized:
                    normalized.update(
                        retrieval_source=job.mode,
                        retrieval_source_id=job.source_id,
                        sector_ids=list(job.sector_ids),
                    )
                    normalized_rows.append(normalized)

        if job.mode == "creator":
            parent_cutoffs = _creator_parent_cutoffs(normalized_rows, job.cutoff)
            allowed_rows = []
            for normalized in normalized_rows:
                reason = _creator_record_cutoff_reason(
                    normalized, job.cutoff, parent_cutoffs,
                )
                if reason:
                    continue
                normalized["published_at"] = _normalized_timestamp(
                    normalized["published_at"]
                )
                allowed_rows.append(normalized)
            normalized_rows = allowed_rows

        ordered_content_ids = list(dict.fromkeys(
            row["content_id"] for row in normalized_rows if not row["comment_id"]
        ))
        ordered_content_ids.extend(
            content_id for content_id in dict.fromkeys(row["content_id"] for row in normalized_rows)
            if content_id not in ordered_content_ids
        )
        allowed_content_ids = set(ordered_content_ids[:max_contents])
        emitted_contents: set[str] = set()
        comment_counts: dict[str, int] = {}
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as target:
            for normalized in normalized_rows:
                content_id = normalized["content_id"]
                if content_id not in allowed_content_ids:
                    continue
                if not normalized["comment_id"]:
                    if content_id in emitted_contents:
                        continue
                    emitted_contents.add(content_id)
                else:
                    count = comment_counts.get(content_id, 0)
                    if count >= max_comments:
                        continue
                    comment_counts[content_id] = count + 1
                target.write(json.dumps(normalized, ensure_ascii=False) + "\n")
    return run


def normalize_external_record(
    raw: dict, platform: str, *, trade_date: str | None = None,
) -> dict | None:
    """Map common MediaCrawler content/comment fields into Apex's canonical contract."""
    content_id = next((raw.get(key) for key in
                       ("content_id", "aweme_id", "video_id", "note_id", "id") if raw.get(key)), None)
    comment_id = next((raw.get(key) for key in ("comment_id", "cid") if raw.get(key)), "")
    text = next((raw.get(key) for key in
                 ("text", "content", "comment_content", "title", "desc") if raw.get(key)), "")
    if not content_id or not text:
        return None
    author = next((raw.get(key) for key in
                   ("creator_hash", "user_id", "author_id", "uid") if raw.get(key)), None)
    raw_engagement = next((
        raw.get(key) for key in ("engagement", "like_count", "liked_count")
        if raw.get(key) not in (None, "")
    ), 0)
    if isinstance(raw_engagement, bool):
        raise ValueError("engagement must be a non-negative finite number")
    engagement = float(raw_engagement)
    if not math.isfinite(engagement) or engagement < 0:
        raise ValueError("engagement must be a non-negative finite number")
    return {
        "platform": platform, "content_id": str(content_id), "comment_id": str(comment_id),
        "published_at": raw.get("published_at") or raw.get("create_time"),
        "collected_at": _now(), "trade_date": trade_date, "text": str(text),
        "title": raw.get("title") or "",
        "description": raw.get("description") or raw.get("desc") or "",
        "tags": raw.get("tags") or [],
        "engagement": engagement,
        "url": raw.get("url") or raw.get("note_url") or raw.get("video_url"),
        "batch_id": raw.get("batch_id"),
        "author_hash": hashlib.sha256(str(author).encode()).hexdigest() if author else None,
        "author_id": str(author) if author else None,
        "author_name": raw.get("author_name") or raw.get("nickname"),
        "retrieval_source": raw.get("retrieval_source") or "search",
    }


def collect_external(*, platforms: list[str], keywords: list[str], raw_dir: str | Path,
                     runner: Callable, trade_date: str | None = None, timeout: int = 900) -> dict:
    trade_date = trade_date or date.today().isoformat()
    base = Path(raw_dir) / trade_date
    base.mkdir(parents=True, exist_ok=True)
    statuses: dict[str, dict[str, Any]] = {}
    for platform in platforms:
        destination = base / f"{platform}-{uuid.uuid4().hex[:10]}.jsonl"
        try:
            runner(platform, keywords, destination, timeout)
            count = sum(1 for line in destination.read_text(encoding="utf-8").splitlines() if line.strip())
            statuses[platform] = {"status": "ok", "records": count, "path": str(destination)}
        except Exception as exc:
            statuses[platform] = {"status": "failed", "records": 0,
                                  "error": f"{type(exc).__name__}: {exc}"}
    ok = sum(value["status"] == "ok" for value in statuses.values())
    return {"trade_date": trade_date, "platforms": statuses,
            "coverage": ok / len(platforms) if platforms else 0.0, "as_of": _now()}


def collect_jobs(jobs: list[RetrievalJob], runner: Callable, raw_dir: str | Path,
                 trade_date: str, limits: dict, timeout: int = 900,
                 empty_coverage: float = 1.0, *,
                 store: SentimentStore | None = None) -> dict:
    """Run typed retrieval jobs independently and retain every successful output."""
    base = Path(raw_dir) / trade_date
    base.mkdir(parents=True, exist_ok=True)
    statuses = []
    for job in jobs:
        if job.trade_date is not None and job.trade_date != trade_date:
            raise ValueError("retrieval job trade_date does not match collection trade_date")
        destination = base / f"{job.platform}-{job.mode}-{uuid.uuid4().hex}.jsonl"
        status = {
            "platform": job.platform, "mode": job.mode, "value": job.value,
            "source_id": job.source_id, "sector_ids": list(job.sector_ids),
            "trade_date": trade_date, "cutoff": job.cutoff,
            "config_hash": job.config_hash, "budget_key": job.budget_key,
        }
        if store is not None and not store.reserve_collection_budget(trade_date, job, limits):
            prior = store.collection_budget(trade_date, job) or {}
            expected_contents = max(0, int(limits.get("max_contents", 0)))
            expected_comments = max(0, int(limits.get("max_comments", 0)))
            compatible = (
                prior.get("config_hash") == job.config_hash
                and prior.get("max_contents") == expected_contents
                and prior.get("max_comments_per_content") == expected_comments
            )
            reused = compatible and prior.get("status") == "completed"
            conflict = not compatible
            statuses.append({
                **status,
                "status": (
                    "budget_config_mismatch" if conflict
                    else "budget_reused" if reused else "budget_exhausted"
                ),
                "records": 0,
            })
            continue
        try:
            runner(job, destination, timeout, limits)
            lines = [line for line in destination.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
            count = len(lines)
            content_ids: set[str] = set()
            comment_count = 0
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("content_id"):
                    content_ids.add(str(record["content_id"]))
                if record.get("comment_id"):
                    comment_count += 1
            statuses.append({**status, "status": "ok", "records": count,
                             "path": str(destination)})
            if store is not None:
                store.complete_collection_budget(
                    trade_date, job, "collected",
                    consumed_contents=len(content_ids), consumed_comments=comment_count,
                )
        except Exception as exc:
            statuses.append({**status, "status": "failed", "records": 0,
                             "error": f"{type(exc).__name__}: {exc}"})
            if store is not None:
                store.complete_collection_budget(trade_date, job, "failed")
    successful = sum(item["status"] in {"ok", "budget_reused"} for item in statuses)
    return {
        "trade_date": trade_date,
        "jobs": statuses,
        "coverage": successful / len(jobs) if jobs else float(empty_coverage),
        "as_of": _now(),
    }


def _ingest_and_classify_relevance(
    report: dict, store: SentimentStore, taxonomy: list[dict], *,
    relevance_llm_classifier: Callable | None = None,
    relevance_llm_enabled: bool = False,
    relevance_version: str = RELEVANCE_VERSION,
    relevance_llm_model: str = "deepseek-chat",
) -> dict:
    """Ingest successful job outputs, then persist finance relevance decisions."""
    sector_terms = list(dict.fromkeys(
        term for sector in taxonomy
        for term in [sector.get("sector_name", ""), *(sector.get("aliases") or [])]
        if term
    ))
    totals = {"inserted": 0, "duplicates": 0}
    raw_recalled = accepted = 0
    source_ids: set[str] = set()
    quarantined = 0
    quarantine_reasons: dict[str, int] = {}
    if relevance_llm_classifier is None:
        def relevance_llm_classifier(record: dict, terms: list[str], version: str):
            return llm_financial_relevance(
                record, terms, version, model=relevance_llm_model,
            )

    def quarantine(reason: str) -> None:
        nonlocal quarantined
        quarantined += 1
        quarantine_reasons[reason] = quarantine_reasons.get(reason, 0) + 1

    for item in report["jobs"]:
        if item["status"] != "ok":
            continue
        try:
            path = Path(item["path"])
            records = []
            decisions = []
            candidates = []
            item_quarantine_start = quarantined
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    quarantine("invalid_json")
                    continue
                if not isinstance(record, dict):
                    quarantine("canonical_record_not_object")
                    continue
                missing = [field for field in ("platform", "content_id", "text")
                           if not record.get(field)]
                if missing:
                    quarantine("canonical_missing_" + "_".join(missing))
                    continue
                record["retrieval_source"] = item["mode"]
                record["retrieval_source_id"] = item["source_id"]
                if item.get("sector_ids"):
                    record["sector_ids"] = item["sector_ids"]
                record["trade_date"] = item.get("trade_date") or report.get("trade_date")
                candidates.append(record)
            parent_cutoffs = (
                _creator_parent_cutoffs(candidates, item.get("cutoff"))
                if item["mode"] == "creator" else {}
            )
            for record in candidates:
                if item["mode"] == "creator":
                    reason = _creator_record_cutoff_reason(
                        record, item.get("cutoff"), parent_cutoffs,
                    )
                    if reason:
                        quarantine(reason)
                        continue
                    record["published_at"] = _normalized_timestamp(record["published_at"])
                try:
                    decision = classify_financial_relevance(
                        record, sector_terms, item["mode"] == "creator",
                    )
                except Exception as exc:
                    decision = RelevanceDecision(
                        0.0, "filtered_non_financial",
                        (f"audit:relevance_rule_error:{type(exc).__name__}",),
                        relevance_version,
                    )
                if decision.decision == "review":
                    if relevance_llm_enabled:
                        try:
                            decision = relevance_llm_classifier(
                                record, sector_terms, relevance_version,
                            )
                        except Exception as exc:
                            decision = RelevanceDecision(
                                0.0, "filtered_non_financial",
                                (*decision.reasons,
                                 f"audit:relevance_llm_error:{type(exc).__name__}"),
                                relevance_version,
                            )
                    else:
                        decision = RelevanceDecision(
                            decision.score, "filtered_non_financial",
                            (*decision.reasons, "audit:relevance_llm_disabled"),
                            relevance_version,
                        )
                records.append(record)
                decisions.append(decision)
            if not records and quarantined > item_quarantine_start:
                reasons = ",".join(sorted(quarantine_reasons))
                raise ValueError(f"all canonical records were quarantined: {reasons}")
            ingested = store.ingest(records, decisions)
            if item.get("budget_key"):
                store.set_collection_budget_status(
                    report["trade_date"], item["budget_key"], "completed",
                )
        except Exception as exc:
            item.update(
                status="failed", records=0,
                error=f"{type(exc).__name__}: {exc}",
            )
            if item.get("budget_key"):
                store.set_collection_budget_status(
                    report["trade_date"], item["budget_key"], "failed",
                )
            continue
        item["quarantined"] = quarantined - item_quarantine_start
        totals = {key: totals[key] + ingested[key] for key in totals}
        raw_recalled += len(records)
        accepted += sum(decision.decision == "accepted" for decision in decisions)
        if records:
            source_ids.add(item["source_id"])
    successful = sum(item["status"] in {"ok", "budget_reused"} for item in report["jobs"])
    if report["jobs"]:
        report["coverage"] = successful / len(report["jobs"])
    return {
        **totals,
        "raw_recalled": raw_recalled,
        "financial_relevant": accepted,
        "sources": len(source_ids),
        "quarantined": quarantined,
        "quarantine_reasons": quarantine_reasons,
    }


def briefing_changes(store: SentimentStore, trade_date: str, coverage: float = 1.0) -> list[str]:
    labels = {"opened": "进入观察", "upgraded": "升级警戒", "resolved": "解除预警"}
    lines = [f'{event["sector_name"]}：{labels[event["event_type"]]}'
             for event in store.list_events() if event["trade_date"] == trade_date]
    if coverage < 1.0:
        lines.append(f"情绪数据覆盖不足（{coverage:.0%}），缺失平台不视为低风险")
    return lines


def _semantic_evidence(record: dict, taxonomy: list[dict]) -> list[dict]:
    text = str(record.get("semantic_text") or _semantic_text(record))
    bullish = ("看多", "起飞", "上车", "加仓", "坚定", "突破", "牛市")
    bearish = ("看空", "跑路", "清仓", "见顶", "暴跌", "退潮", "割肉")
    fomo_words = ("必须", "赶紧", "梭哈", "错过", "上车", "起飞")
    panic_words = ("快跑", "割肉", "崩盘", "清仓", "暴跌")
    stance = 1.0 if any(word in text for word in bullish) else -1.0 if any(word in text for word in bearish) else 0.0
    confidence = 0.9 if stance else 0.5
    output = []
    for sector in taxonomy:
        aliases = sector.get("aliases") or [sector.get("sector_name", "")]
        explicit_mapping = (
            record.get("platform") == "eastmoney"
            and sector["sector_id"] in (record.get("sector_ids") or [])
        )
        if not explicit_mapping and not any(alias and alias in text for alias in aliases):
            continue
        output.append({
            "platform": record["platform"], "content_id": record["content_id"],
            "comment_id": record.get("comment_id", ""), "author_hash": record.get("author_hash"),
            "text": text, "engagement": record.get("engagement", 0),
            "sector_id": sector["sector_id"], "sector_name": sector["sector_name"],
            "taxonomy": sector["taxonomy"], "stance": stance, "confidence": confidence,
            "fomo": float(any(word in text for word in fomo_words)),
            "panic": float(any(word in text for word in panic_words)),
            "mapping_confidence": 0.99 if explicit_mapping else 0.95,
            "narrative": next((a for a in aliases if a in text), sector["sector_name"]),
            "evidence_span": text[:160],
            "repost_weight": float(record.get("repost_weight", 1.0)),
            "source_type": record.get("source_type"),
            "stock_code": record.get("stock_code"),
            "url": record.get("url"),
        })
    return output


def _llm_json(reply: Any, label: str) -> Any:
    if not isinstance(reply, dict) or not isinstance(reply.get("content"), str):
        raise ValueError(f"{label} response must contain text content")
    content = reply["content"].strip()
    if content.startswith("```json"):
        content = content[len("```json"):]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    try:
        return json.loads(content.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} response is not valid JSON") from exc


def _strict_finite_number(value: Any, low: float, high: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{label} must be finite and in [{low},{high}]")
    return result


def llm_financial_relevance(
    record: dict, sector_terms: list[str], version: str, *,
    model: str = "deepseek-chat",
) -> RelevanceDecision:
    """Resolve only the finance gate; the schema cannot emit alert-scoring fields."""
    from apex import llm

    prompt = (
        "只返回一个JSON对象，且只能包含 financial_relevant(bool), confidence(0到1), "
        "reasons(非空字符串数组)。不得输出板块观点、情绪、预警或交易建议。\n"
        f"候选实体={json.dumps(sector_terms, ensure_ascii=False)}\n"
        f"已脱敏文本={_semantic_text(record)}"
    )
    reply = llm.chat(
        [{"role": "system", "content": f"relevance_prompt_version={version}"},
         {"role": "user", "content": prompt}],
        temperature=0, max_tokens=500, model=model,
    )
    value = _llm_json(reply, "relevance LLM")
    expected = {"financial_relevant", "confidence", "reasons"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("relevance LLM schema contains missing or unknown fields")
    relevant = value["financial_relevant"]
    if type(relevant) is not bool:
        raise ValueError("relevance LLM financial_relevant must be boolean")
    confidence = _strict_finite_number(
        value["confidence"], 0.0, 1.0, "relevance LLM confidence",
    )
    reasons = value["reasons"]
    if (not isinstance(reasons, list) or not reasons
            or any(not isinstance(reason, str) or not reason.strip() for reason in reasons)):
        raise ValueError("relevance LLM reasons must be a non-empty string array")
    return RelevanceDecision(
        confidence if relevant else 1.0 - confidence,
        "accepted" if relevant else "filtered_non_financial",
        tuple(f"llm:{reason.strip()}" for reason in reasons),
        version,
    )


def llm_semantic_evidence(
    record: dict, taxonomy: list[dict], *, model: str = "deepseek-chat",
) -> list[dict]:
    """Classify a low-confidence text into the fixed evidence contract."""
    from apex import llm
    allowed = [{"sector_id": item["sector_id"], "sector_name": item["sector_name"],
                "taxonomy": item["taxonomy"], "aliases": item.get("aliases", [])}
               for item in taxonomy]
    semantic_text = str(record.get("semantic_text") or _semantic_text(record))
    prompt = (
        "只返回JSON数组。对文本做板块映射与散户情绪分类。每项字段必须为: "
        "sector_id, stance(-1到1), confidence(0到1), fomo(0到1), panic(0到1), "
        "narrative, evidence_span。sector_id只能来自候选。\n"
        f"候选={json.dumps(allowed, ensure_ascii=False)}\n文本={semantic_text}"
    )
    reply = llm.chat(
        [{"role": "system", "content": f"prompt_version={PROMPT_VERSION}"},
         {"role": "user", "content": prompt}],
        temperature=0, max_tokens=1200, model=model,
    )
    values = _llm_json(reply, "semantic LLM")
    if not isinstance(values, list):
        raise ValueError("semantic LLM root must be an array")
    by_id = {item["sector_id"]: item for item in taxonomy}
    output = []
    seen_sector_ids: set[str] = set()
    expected = {
        "sector_id", "stance", "confidence", "fomo", "panic",
        "narrative", "evidence_span",
    }
    for value in values:
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("semantic LLM schema contains missing or unknown fields")
        meta = by_id.get(value["sector_id"])
        if meta is None:
            raise ValueError("semantic LLM sector_id is outside the taxonomy whitelist")
        if value["sector_id"] in seen_sector_ids:
            raise ValueError("semantic LLM returned a duplicate sector_id")
        seen_sector_ids.add(value["sector_id"])
        stance = _strict_finite_number(value["stance"], -1.0, 1.0, "semantic LLM stance")
        confidence = _strict_finite_number(
            value["confidence"], 0.0, 1.0, "semantic LLM confidence",
        )
        fomo = _strict_finite_number(value["fomo"], 0.0, 1.0, "semantic LLM fomo")
        panic = _strict_finite_number(value["panic"], 0.0, 1.0, "semantic LLM panic")
        if not isinstance(value["narrative"], str) or not isinstance(value["evidence_span"], str):
            raise ValueError("semantic LLM narrative and evidence_span must be strings")
        if not value["evidence_span"] or value["evidence_span"] not in semantic_text:
            raise ValueError("semantic LLM evidence_span must be a non-empty source substring")
        output.append({
            "sector_id": value["sector_id"], "stance": stance,
            "confidence": confidence, "fomo": fomo, "panic": panic,
            "narrative": _redact_text(value["narrative"], 160),
            "evidence_span": _redact_text(value["evidence_span"], 160),
            "platform": record["platform"], "content_id": record["content_id"],
            "comment_id": record.get("comment_id", ""), "author_hash": record.get("author_hash"),
            "text": _redact_text(value["evidence_span"], 160),
            "engagement": record.get("engagement", 0),
            "sector_name": meta["sector_name"], "taxonomy": meta["taxonomy"],
            "mapping_confidence": confidence,
            "repost_weight": float(record.get("repost_weight", 1.0)),
            "source_type": record.get("source_type"),
            "stock_code": record.get("stock_code"),
            "url": (record.get("url") if record.get("platform") == "eastmoney"
                    and str(record.get("url") or "").startswith(
                        "https://guba.eastmoney.com/"
                    ) else None),
        })
    return output


def _eastmoney_promotion_audit(
    store: SentimentStore, phase: str, override_reason: str,
) -> dict[str, Any] | None:
    if phase != "promoted":
        return None
    progress = store.eastmoney_shadow_progress()
    observed = int(progress["qualified_days"])
    eligible = observed >= EASTMONEY_PROMOTION_QUALIFIED_DAYS
    reason = override_reason.strip()
    override_used = not eligible and bool(reason)
    audit = {
        "required_qualified_days": EASTMONEY_PROMOTION_QUALIFIED_DAYS,
        "observed_qualified_days": observed,
        "eligible_by_history": eligible,
        "override_used": override_used,
        "override_reason": reason if override_used else "",
    }
    if not eligible and not override_used:
        raise ValueError(
            "eastmoney promotion requires 14 quality-qualified shadow dates "
            "or an explicit audited override reason"
        )
    return audit


def run_configured(cfg: dict | None = None, *, runner: Callable | None = None,
                   eastmoney_runner: Callable | None = None,
                   eastmoney_constituent_provider: Callable | None = None,
                   trade_date: str | None = None, market_provider: Callable | None = None,
                   semantic_classifier: Callable | None = None) -> dict:
    """Run external collection and normalized ingestion from application config.

    Platform-specific output normalization remains an adapter responsibility: each
    emitted JSONL row must follow the canonical content contract consumed by ingest.
    """
    if cfg is None:
        from apex import config
        cfg = config.get() or {}
    settings = cfg.get("sector_sentiment") or {}
    if not settings.get("enabled", False):
        return {"status": "disabled"}
    cache_dir = Path(str(settings.get("cache_dir", "~/.stock-sentiment"))).expanduser()
    if market_provider is None and settings.get("market_data_enabled", False):
        market_provider = fetch_sector_market
    taxonomy = list(settings.get("taxonomy", []))
    if not taxonomy:
        taxonomy = [{"sector_id": f"concept:{word}", "sector_name": word,
                     "taxonomy": "concept", "aliases": [word]}
                    for word in settings.get("keywords", [])]
    platforms = list(settings.get("platforms", ["bili", "dy"]))
    eastmoney_settings = dict(settings.get("eastmoney") or {})
    eastmoney_enabled = bool(eastmoney_settings.get("enabled", False))
    eastmoney_phase = str(eastmoney_settings.get("phase", "shadow"))
    eastmoney_promoted = eastmoney_enabled and eastmoney_phase == "promoted"
    media_platforms = [platform for platform in platforms if platform != "eastmoney"]
    store = open_store(cache_dir)
    promotion_audit = None
    if eastmoney_enabled:
        from apex.eastmoney_guba.pipeline import validate_eastmoney_config
        validate_eastmoney_config(eastmoney_settings)
        promotion_audit = _eastmoney_promotion_audit(
            store, eastmoney_phase,
            str(eastmoney_settings.get("promotion_override_reason") or ""),
        )
    retrieval_settings = dict(settings.get("retrieval") or {})
    policy_hash = _validate_retrieval_policy(settings, retrieval_settings)
    retrieval_settings["config_hash"] = policy_hash
    timeout = int(settings.get("timeout_seconds", 900))
    limits = {
        "max_contents": int(retrieval_settings.get("max_contents_per_query", 20)),
        "max_comments": int(retrieval_settings.get("max_comments_per_content", 50)),
    }
    resolved_date = trade_date or date.today().isoformat()
    search_jobs = build_search_jobs(
        taxonomy, media_platforms, retrieval_settings,
        trade_date=resolved_date, config_hash=policy_hash,
    )
    if runner is None:
        external_root = Path(str(settings["mediacrawler_path"])).expanduser()
        runner = default_mediacrawler_runner(
            external_root,
            settings.get("mediacrawler_commit"),
            retrieval_settings.get("creator_id_argument", "--creator_id"),
        )
    raw_dir = cache_dir / "raw"
    policy_cohort = store.reserve_policy_day(resolved_date, policy_hash)
    relevance_llm_enabled = bool(retrieval_settings.get(
        "relevance_llm_enabled", settings.get("llm_enabled", False),
    ))

    search_report = collect_jobs(
        search_jobs, runner, raw_dir, resolved_date, limits, timeout,
        empty_coverage=0.0, store=store,
    )
    search_ingest = _ingest_and_classify_relevance(
        search_report, store, taxonomy,
        relevance_llm_enabled=relevance_llm_enabled,
        relevance_version=retrieval_settings["relevance_version"],
        relevance_llm_model=str(settings.get("relevance_llm_model") or "deepseek-chat"),
    )
    store.discover_creator_candidates(store.search_records(resolved_date), retrieval_settings)
    creator_jobs = build_creator_jobs(
        store.approved_creators(), media_platforms,
        trade_date=resolved_date, config_hash=policy_hash,
    )
    creator_report = collect_jobs(
        creator_jobs, runner, raw_dir, resolved_date, limits, timeout, store=store,
    )
    creator_ingest = _ingest_and_classify_relevance(
        creator_report, store, taxonomy,
        relevance_llm_enabled=relevance_llm_enabled,
        relevance_version=retrieval_settings["relevance_version"],
        relevance_llm_model=str(settings.get("relevance_llm_model") or "deepseek-chat"),
    )
    for item in creator_report["jobs"]:
        if item["status"] == "failed":
            store.set_creator_collection_error(
                item["platform"], item["value"], item.get("error"),
            )
        elif item["status"] in {"ok", "budget_reused"}:
            store.set_creator_collection_error(item["platform"], item["value"], None)
    totals = {
        key: search_ingest[key] + creator_ingest[key]
        for key in ("inserted", "duplicates")
    }
    eastmoney_telemetry = {
        "platform": "eastmoney", "status": "disabled", "posts": 0,
        "comments": 0, "independent_authors": 0, "parse_success_rate": 0.0,
    }
    if promotion_audit is not None:
        eastmoney_telemetry["promotion_audit"] = promotion_audit
    eastmoney_report = {"trade_date": resolved_date, "jobs": []}
    if eastmoney_enabled:
        from apex.eastmoney_guba import CollectionResult, EastmoneyRunner
        from apex.eastmoney_guba.pipeline import collect_eastmoney
        from apex.eastmoney_guba.representatives import (
            describe_provider_failure,
            describe_injected_constituents,
            normalize_injected_representative_provenance,
            select_representative_constituents,
        )
        if eastmoney_runner is None:
            eastmoney_runner = EastmoneyRunner().run
        try:
            if eastmoney_constituent_provider is None:
                provider_result = select_representative_constituents(
                    taxonomy, resolved_date, eastmoney_settings["constituent_count"],
                )
            else:
                provider_result = eastmoney_constituent_provider(taxonomy, resolved_date)
            if (isinstance(provider_result, tuple) and len(provider_result) == 2):
                constituents, representative_provenance = provider_result
            else:
                constituents = provider_result
                representative_provenance = describe_injected_constituents(
                    taxonomy, constituents, trade_date=resolved_date,
                    constituent_count=eastmoney_settings["constituent_count"],
                ) if isinstance(constituents, dict) else {}
            if not isinstance(constituents, dict):
                raise ValueError("eastmoney constituent provider must return a sector mapping")
            if not isinstance(representative_provenance, dict):
                raise ValueError("eastmoney representative provenance must be a sector mapping")
            if isinstance(provider_result, tuple):
                representative_provenance = normalize_injected_representative_provenance(
                    taxonomy, constituents, representative_provenance,
                    trade_date=resolved_date,
                    constituent_count=eastmoney_settings["constituent_count"],
                )
            collection_runner = eastmoney_runner
        except Exception:
            constituents, representative_provenance = describe_provider_failure(
                taxonomy, trade_date=resolved_date,
                constituent_count=eastmoney_settings["constituent_count"],
            )

            def collection_runner(manifest_path, report_path, *args, **kwargs):
                with Path(report_path).open("x", encoding="utf-8") as stream:
                    json.dump({
                        "parser_successes": 0, "parser_errors": 0,
                        "request_successes": 0, "request_errors": 0,
                        "reason": "representative_provider_unavailable",
                    }, stream, ensure_ascii=False, sort_keys=True)
                return CollectionResult(
                    "failed", 0, 0, len(taxonomy),
                    message="representative_provider_unavailable",
                )
        try:
            eastmoney_telemetry, eastmoney_report = collect_eastmoney(
                cache_dir=cache_dir, config=eastmoney_settings, taxonomy=taxonomy,
                constituents=constituents, representative_provenance=representative_provenance,
                trade_date=resolved_date, collected_at=_now(), runner=collection_runner,
                promotion_audit=promotion_audit,
            )
            eastmoney_telemetry["phase"] = eastmoney_phase
            eastmoney_ingest = _ingest_and_classify_relevance(
                eastmoney_report, store, taxonomy,
                relevance_llm_enabled=relevance_llm_enabled,
                relevance_version=retrieval_settings["relevance_version"],
                relevance_llm_model=str(settings.get("relevance_llm_model") or "deepseek-chat"),
            )
            for key in totals:
                totals[key] += eastmoney_ingest[key]
        except Exception:
            eastmoney_telemetry.update(
                status="degraded", current_status="degraded", display_status="degraded",
                phase=eastmoney_phase, target_shortfall=True,
                message="eastmoney_collection_unavailable",
            )
    if eastmoney_enabled:
        eastmoney_telemetry["phase"] = eastmoney_phase
        if promotion_audit is not None:
            eastmoney_telemetry["promotion_audit"] = promotion_audit
        eastmoney_telemetry["shadow_qualified"] = bool(
            eastmoney_telemetry.get("status") in {"ok", "empty_valid"}
            and int(eastmoney_telemetry.get("posts") or 0)
            >= int(eastmoney_settings.get("minimum_posts", 1))
            and int(eastmoney_telemetry.get("independent_authors") or 0)
            >= int(eastmoney_settings.get("minimum_authors", 1))
            and float(eastmoney_telemetry.get("parse_success_rate") or 0)
            >= float(eastmoney_settings.get("minimum_parse_success_rate", 1.0))
        )
    funnel = store.retrieval_funnel(resolved_date)
    all_jobs = search_report["jobs"] + creator_report["jobs"] + eastmoney_report["jobs"]
    successful_jobs = sum(
        item["status"] in {"ok", "budget_reused"} for item in all_jobs
    )
    if eastmoney_promoted:
        from apex.eastmoney_guba.pipeline import grouped_coverage
        coverage_groups = grouped_coverage(
            search_report["jobs"] + creator_report["jobs"],
            eastmoney_telemetry, eastmoney_settings,
        )
        overall_coverage = 1.0 if coverage_groups["qualified"] else 0.0
    else:
        media_jobs = search_report["jobs"] + creator_report["jobs"]
        media_successes = sum(item.get("status") in {"ok", "budget_reused"}
                              for item in media_jobs)
        coverage_groups = {"short_video": bool(media_successes),
                           "finance_community": False, "qualified": False}
        overall_coverage = (
            media_successes / len(media_jobs) if media_jobs and search_jobs else 0.0
        )
    report = {
        "trade_date": resolved_date,
        "search_coverage": search_report["coverage"],
        "creator_coverage": creator_report["coverage"],
        "coverage": overall_coverage,
        "jobs": all_jobs,
        "search_jobs": search_report["jobs"],
        "creator_jobs": creator_report["jobs"],
        "platforms": {
            platform: {
                "status": ("ok" if any(
                    job.get("platform") == platform
                    and job.get("status") in {"ok", "budget_reused"}
                    for job in all_jobs
                ) else "failed")
            }
            for platform in media_platforms
        } | ({"eastmoney": eastmoney_telemetry} if eastmoney_enabled else {}),
        "eastmoney": eastmoney_telemetry,
        "coverage_groups": coverage_groups,
        "funnel": funnel,
        "query_version": retrieval_settings["query_version"],
        "relevance_version": retrieval_settings["relevance_version"],
        "creator_rule_version": retrieval_settings["creator_rule_version"],
        "semantic_prompt_version": settings["semantic_prompt_version"],
        "config_hash": policy_hash,
        **policy_cohort,
        "as_of": _now(),
    }
    store.record_collection(report)
    all_records = store.list_eligible_content(resolved_date)
    scoring_records = (all_records if eastmoney_promoted else
                       [record for record in all_records
                        if record.get("platform") != "eastmoney"])
    if semantic_classifier is None:
        if settings.get("llm_enabled", False):
            def semantic_classifier(record, taxonomy):
                baseline = _semantic_evidence(record, taxonomy)
                if baseline and all(item["stance"] != 0 for item in baseline):
                    return baseline
                return llm_semantic_evidence(
                    record, taxonomy,
                    model=str(settings.get("semantic_llm_model") or "deepseek-chat"),
                )
        else:
            semantic_classifier = _semantic_evidence
    evidence = []
    classification_errors = 0
    for record in scoring_records:
        try:
            evidence.extend(semantic_classifier(record, taxonomy))
        except Exception:
            classification_errors += 1
            evidence.extend(_semantic_evidence(record, taxonomy))
    by_sector: dict[str, list[dict]] = {}
    for item in evidence:
        by_sector.setdefault(item["sector_id"], []).append(item)
    market_metrics = settings.get("market_metrics") or {}
    for sector_id, rows in by_sector.items():
        meta = rows[0]
        history = [
            row for row in store.scores_for_sector(sector_id)
            if row["trade_date"] < resolved_date
        ]
        market = market_metrics.get(sector_id, {})
        if market_provider:
            try:
                lookback = (datetime.fromisoformat(resolved_date) - timedelta(days=30)).date().isoformat()
                series = market_provider(meta["sector_name"], meta["taxonomy"], lookback, resolved_date)
                if series:
                    market = series[-1]
            except Exception:
                market = {}
        short_ok = any(row.get("platform") in {"bili", "dy"} for row in rows)
        east_rows = [row for row in rows if row.get("platform") == "eastmoney"]
        east_posts = sum(not row.get("comment_id") for row in east_rows)
        east_authors = len({row.get("author_hash") for row in east_rows if row.get("author_hash")})
        finance_ok = (eastmoney_promoted
                      and eastmoney_telemetry.get("status") in {"ok", "empty_valid"}
                      and east_posts >= int(eastmoney_settings.get("minimum_posts", 1))
                      and east_authors >= int(eastmoney_settings.get("minimum_authors", 1))
                      and float(eastmoney_telemetry.get("parse_success_rate") or 0)
                      >= float(eastmoney_settings.get("minimum_parse_success_rate", 1.0)))
        quality = {
            "short_video": short_ok, "finance_community": finance_ok,
            # Shadow must preserve the pre-promotion short-video state behavior.
            # It cannot satisfy or newly fail the finance-community gate.
            "qualified": short_ok and finance_ok if eastmoney_promoted else False,
            "eastmoney_status": eastmoney_telemetry.get("status"),
            "eastmoney_posts": east_posts, "eastmoney_authors": east_authors,
            "eastmoney_parse_success_rate": float(
                eastmoney_telemetry.get("parse_success_rate") or 0
            ),
        }
        if not eastmoney_promoted:
            quality = {
                "short_video": short_ok,
                "finance_community": False,
                # Preserve the legacy gate: the existing platform/author/mapping checks
                # in _coverage_ok remain authoritative until explicit promotion.
                "qualified": True,
            }
        score = build_daily_score(
            trade_date=resolved_date, sector_id=sector_id, sector_name=meta["sector_name"],
            taxonomy=meta["taxonomy"], evidence=rows, history=history,
            market=market, coverage_quality=quality,
        )
        score["model_version"] = (settings.get("llm_model_version", "sector-semantic-llm-v1")
                                  if settings.get("llm_enabled", False) else MODEL_VERSION)
        store.save_daily_score(score)
    changes = advance_alerts(store, resolved_date)
    labeled = 0
    if market_provider:
        opened = [event for event in store.list_events() if event["event_type"] == "opened"]
        for event in opened:
            try:
                lookback = (datetime.fromisoformat(event["trade_date"]) - timedelta(days=30)).date().isoformat()
                series = market_provider(event["sector_name"], event["taxonomy"], lookback, resolved_date)
                signal_scores = [row for row in store.scores_for_date(event["trade_date"])
                                 if row["sector_id"] == event["sector_id"]]
                heat_baseline = bool(signal_scores and signal_scores[0]["sentiment_extreme"] >= 0.90)
                for row in series:
                    if row["trade_date"] == event["trade_date"]:
                        row["heat_baseline_hit"] = heat_baseline
                if len([row for row in series if row["trade_date"] > event["trade_date"]]) >= 3:
                    label_event(store, event["sector_id"], event["trade_date"], series)
                    labeled += 1
            except Exception:
                continue
    return {"status": "ok" if report["coverage"] == 1 else "degraded",
            "coverage": report["coverage"], "collection": report, "ingest": totals,
            "funnel": funnel,
            "sectors_scored": len(by_sector), "alerts": changes, "events_labeled": labeled,
            "classification_errors": classification_errors}


def fetch_sector_market(sector_name: str, taxonomy: str, start_date: str, end_date: str) -> list[dict]:
    """Fetch Eastmoney board returns; benchmark can be supplied by a richer provider later."""
    import akshare as ak
    start = start_date.replace("-", "")
    end = end_date.replace("-", "")
    if taxonomy == "industry":
        frame = ak.stock_board_industry_hist_em(symbol=sector_name, start_date=start, end_date=end,
                                                period="日k", adjust="")
    else:
        frame = ak.stock_board_concept_hist_em(symbol=sector_name, start_date=start, end_date=end,
                                               period="日k", adjust="")
    if frame is None or frame.empty:
        return []
    benchmark_by_date: dict[str, float] = {}
    try:
        benchmark = ak.stock_zh_index_daily_em(symbol="sh000300", start_date=start, end_date=end)
        if benchmark is not None and not benchmark.empty:
            closes = benchmark["close"].astype(float)
            changes = closes.pct_change().fillna(0) * 100
            benchmark_by_date = {str(day)[:10]: float(change)
                                 for day, change in zip(benchmark["date"], changes)}
    except Exception:
        benchmark_by_date = {}
    breadth = None
    fund_flow = None
    try:
        constituents = (ak.stock_board_industry_cons_em(symbol=sector_name) if taxonomy == "industry"
                        else ak.stock_board_concept_cons_em(symbol=sector_name))
        if constituents is not None and not constituents.empty and "涨跌幅" in constituents:
            changes = constituents["涨跌幅"].astype(float)
            breadth = float((changes > 0).sum() / len(changes))
    except Exception:
        breadth = None
    try:
        flow = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        if flow is not None and not flow.empty:
            name_column = "名称" if "名称" in flow else "行业"
            amount_column = next((column for column in flow.columns if "主力净流入" in str(column) and "净额" in str(column)), None)
            matched = flow[flow[name_column] == sector_name]
            if amount_column and not matched.empty:
                fund_flow = float(matched.iloc[0][amount_column]) / 100_000_000
    except Exception:
        fund_flow = None
    rows = []
    returns = [float(value) for value in frame["涨跌幅"].fillna(0).tolist()]
    volumes = [float(value) for value in frame["成交额"].fillna(0).tolist()]
    for index, record in frame.reset_index(drop=True).iterrows():
        previous_volume = volumes[index - 1] if index else 0
        prior = returns[max(0, index - 5):index]
        rows.append({
            "trade_date": str(record["日期"])[:10], "sector_return": returns[index],
            "benchmark_return": benchmark_by_date.get(str(record["日期"])[:10], 0.0),
            "return": returns[index],
            "volume_change": ((volumes[index] / previous_volume) - 1) if previous_volume else 0.0,
            "breadth": breadth, "fund_flow": fund_flow,
            "price_baseline_hit": bool(prior and sum(prior) >= 5.0),
            "heat_baseline_hit": None,
        })
    return rows


def open_store(cache_dir: str | Path) -> SentimentStore:
    return SentimentStore(Path(cache_dir).expanduser() / "sector_sentiment.sqlite3")
