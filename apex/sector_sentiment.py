"""Sector-level retail sentiment shadow alerts.

This module owns normalized storage, deterministic scoring state and external
crawler boundaries. Platform-specific crawling remains in separately installed
tools (for example MediaCrawler); Apex consumes their JSONL output only.
"""
from __future__ import annotations

import hashlib
import json
import re
import random
import sqlite3
import subprocess
import uuid
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from apex.sector_retrieval import (
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(text: str) -> str:
    normalized = re.sub(r"\W+", "", (text or "").lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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
                    collected_at TEXT NOT NULL, text TEXT NOT NULL,
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
                    search_coverage REAL NOT NULL DEFAULT 0,
                    creator_coverage REAL NOT NULL DEFAULT 0,
                    platforms_json TEXT NOT NULL, funnel_json TEXT NOT NULL DEFAULT '{}',
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
                    reviewed_at TEXT, last_collection_error TEXT,
                    PRIMARY KEY(platform, creator_id)
                );
                CREATE TABLE IF NOT EXISTS creator_content (
                    platform TEXT NOT NULL, creator_id TEXT NOT NULL, content_id TEXT NOT NULL,
                    relevance_score REAL NOT NULL, sector_ids_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(platform, creator_id, content_id)
                );
                CREATE TABLE IF NOT EXISTS creator_events (
                    event_id TEXT PRIMARY KEY, platform TEXT NOT NULL, creator_id TEXT NOT NULL,
                    action TEXT NOT NULL, previous_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    actor TEXT NOT NULL, created_at TEXT NOT NULL
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
            }
            for name, definition in migrations.items():
                if name not in content_columns:
                    conn.execute(f"ALTER TABLE content ADD COLUMN {name} {definition}")
            outcome_columns = {row["name"] for row in conn.execute("PRAGMA table_info(outcomes)")}
            for name in ("price_baseline_hit", "heat_baseline_hit"):
                if name not in outcome_columns:
                    conn.execute(f"ALTER TABLE outcomes ADD COLUMN {name} INTEGER")
            collection_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(collection_runs)")
            }
            collection_migrations = {
                "search_coverage": "REAL NOT NULL DEFAULT 0",
                "creator_coverage": "REAL NOT NULL DEFAULT 0",
                "funnel_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, definition in collection_migrations.items():
                if name not in collection_columns:
                    conn.execute(f"ALTER TABLE collection_runs ADD COLUMN {name} {definition}")

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
                        (platform, content_id, comment_id, published_at, collected_at, text,
                         engagement, url, batch_id, author_hash, fingerprint, retrieval_source,
                         retrieval_source_id, retrieval_sector_ids_json, author_id, author_name)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (item["platform"], str(item["content_id"]), str(item.get("comment_id") or ""),
                         item.get("published_at"), item.get("collected_at") or _now(), item.get("text", ""),
                         float(item.get("engagement") or 0), item.get("url"), item.get("batch_id"),
                         item.get("author_hash"), fp, item.get("retrieval_source", "search"),
                         item.get("retrieval_source_id"),
                         json.dumps(item.get("sector_ids", []), ensure_ascii=False),
                         item.get("author_id"), item.get("author_name")),
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
                    duplicates += 1
                    existing = conn.execute(
                        """SELECT retrieval_sector_ids_json FROM content
                           WHERE platform=? AND content_id=? AND comment_id=?""",
                        (item["platform"], str(item["content_id"]),
                         str(item.get("comment_id") or "")),
                    ).fetchone()
                    if existing:
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
                     json.dumps(decision.reasons, ensure_ascii=False), decision.version,
                     item["platform"], str(item["content_id"]),
                     str(item.get("comment_id") or "")),
                )
        return {"inserted": inserted, "duplicates": duplicates}

    def list_content(self, trade_date: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM content WHERE substr(collected_at,1,10)=? ORDER BY id", (trade_date,)
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
                 json.dumps(decision.reasons, ensure_ascii=False), decision.version,
                 platform, str(content_id), str(comment_id or "")),
            )

    def list_eligible_content(self, trade_date: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM content
                   WHERE substr(collected_at,1,10)=? AND relevance_decision='accepted'
                   ORDER BY id""", (trade_date,)
            ).fetchall()
        return [dict(row) for row in rows]

    def accepted_search_records(self, trade_date: str) -> list[dict]:
        """Return accepted search discoveries with their retrieval sector scope."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM content
                   WHERE substr(collected_at,1,10)=? AND relevance_decision='accepted'
                     AND retrieval_source='search'
                   ORDER BY id""", (trade_date,)
            ).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            record["sector_ids"] = json.loads(record.pop("retrieval_sector_ids_json") or "[]")
        return records

    def discover_creator_candidates(self, records: list[dict], settings: dict) -> dict[str, int]:
        """Persist eligible creator evidence and add, but never approve, candidates.

        Each source content key can count at most once.  Discovery deliberately
        leaves reviewed records in their current state: approval is a human-only
        transition performed by :meth:`moderate_creator`.
        """
        minimum = int(settings.get("candidate_min_contents", 3))
        minimum_sectors = int(settings.get("candidate_min_sectors", 2))
        very_high = float(settings.get("very_high_relevance", 0.9))
        thresholds = settings.get(
            "platform_engagement_thresholds", settings.get("high_engagement_thresholds", {})
        )
        created = updated = 0
        seen_creators: dict[tuple[str, str], list[dict]] = {}
        with self._connect() as conn:
            for record in records:
                platform = str(record.get("platform") or "")
                creator_id = str(record.get("author_id") or "")
                content_id = str(record.get("content_id") or "")
                score = float(record.get("relevance_score") or 0)
                decision = record.get("relevance_decision")
                eligible = decision == "accepted" if decision is not None else score >= 0.7
                if not (platform and creator_id and content_id and eligible):
                    continue
                sectors = sorted({str(value) for value in record.get("sector_ids", []) if value})
                conn.execute(
                    """INSERT OR IGNORE INTO creator_content
                    (platform, creator_id, content_id, relevance_score, sector_ids_json)
                    VALUES (?, ?, ?, ?, ?)""",
                    (platform, creator_id, content_id, score, json.dumps(sectors, ensure_ascii=False)),
                )
                seen_creators.setdefault((platform, creator_id), []).append(record)

            for (platform, creator_id), creator_records in seen_creators.items():
                evidence_rows = conn.execute(
                    """SELECT content_id, relevance_score, sector_ids_json FROM creator_content
                    WHERE platform=? AND creator_id=? ORDER BY content_id""",
                    (platform, creator_id),
                ).fetchall()
                sector_ids = sorted({sector for row in evidence_rows
                                     for sector in json.loads(row["sector_ids_json"])})
                valid_count = len(evidence_rows)
                configured_threshold = thresholds.get(
                    platform, settings.get("high_engagement_threshold", float("inf"))
                ) if isinstance(thresholds, dict) else settings.get("high_engagement_threshold", thresholds)
                if isinstance(configured_threshold, dict):
                    configured_threshold = configured_threshold.get("high_engagement_threshold", float("inf"))
                platform_threshold = float(configured_threshold)
                high_signal = any(
                    float(record.get("relevance_score") or 0) >= very_high and
                    float(record.get("engagement") or 0) >= platform_threshold
                    for record in creator_records
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
                evidence = [{"content_id": row["content_id"],
                             "text": str(next((record.get("text") for record in creator_records
                                               if str(record.get("content_id")) == row["content_id"]), ""))[:200]}
                            for row in evidence_rows[-3:]]
                if existing:
                    conn.execute(
                        """UPDATE creators SET display_name=COALESCE(?, display_name),
                           last_discovered_at=?, valid_content_count=?, total_content_count=?,
                           financial_ratio=?, sector_ids_json=?, evidence_json=?
                           WHERE platform=? AND creator_id=?""",
                        (display_name, now, valid_count, valid_count, 1.0 if valid_count else 0.0,
                         json.dumps(sector_ids, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False),
                         platform, creator_id),
                    )
                    updated += 1
                else:
                    conn.execute(
                        """INSERT INTO creators
                        (platform, creator_id, display_name, status, first_discovered_at,
                         last_discovered_at, valid_content_count, total_content_count,
                         financial_ratio, sector_ids_json, evidence_json)
                        VALUES (?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?)""",
                        (platform, creator_id, display_name, now, now, valid_count, valid_count,
                         1.0 if valid_count else 0.0, json.dumps(sector_ids, ensure_ascii=False),
                         json.dumps(evidence, ensure_ascii=False)),
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
        return [creator for creator in creators if platform is None or creator["platform"] == platform]

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
                raise KeyError((platform, creator_id))
            now = _now()
            new_status = transitions[action]
            conn.execute(
                "UPDATE creators SET status=?, reviewed_at=? WHERE platform=? AND creator_id=?",
                (new_status, now, platform, creator_id),
            )
            conn.execute(
                """INSERT INTO creator_events
                (event_id, platform, creator_id, action, previous_status, new_status, actor, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex, platform, creator_id, action, row["status"], new_status, actor, now),
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
                 json.dumps(score.get("evidence", []), ensure_ascii=False),
                 score.get("model_version", MODEL_VERSION), score.get("rule_version", RULE_VERSION),
                 float(score.get("attention_raw", 0)), float(score.get("sentiment_raw", 0))),
            )
            conn.execute(
                """UPDATE daily_scores SET content_count=?, comment_count=?, engagement_raw=?,
                   author_count=?, market_data_complete=?
                   WHERE trade_date=? AND sector_id=?""",
                (float(score.get("content_count", 0)), float(score.get("comment_count", 0)),
                 float(score.get("engagement_raw", 0)),
                 float(score.get("author_count", score.get("independent_authors", 0))),
                 int(bool(score.get("market_data_complete", True))),
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
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO collection_runs
                (run_id, trade_date, coverage, search_coverage, creator_coverage,
                 platforms_json, funnel_json, status, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex, report["trade_date"], float(report["coverage"]),
                 float(report.get("search_coverage", report["coverage"])),
                 float(report.get("creator_coverage", report["coverage"])),
                 json.dumps(report.get("platforms", {}), ensure_ascii=False),
                 json.dumps(report.get("funnel", {}), ensure_ascii=False),
                 "ok" if report["coverage"] == 1 else "degraded", _now()),
            )

    def collection_summary(self, trade_date: str | None = None) -> dict:
        with self._connect() as conn:
            if trade_date:
                row = conn.execute(
                    "SELECT * FROM collection_runs WHERE trade_date=? ORDER BY completed_at DESC LIMIT 1",
                    (trade_date,),
                ).fetchone()
            else:
                row = conn.execute("SELECT * FROM collection_runs ORDER BY trade_date DESC, completed_at DESC LIMIT 1").fetchone()
            days = conn.execute("SELECT count(DISTINCT trade_date) AS n FROM collection_runs").fetchone()["n"]
        if not row:
            return {"trading_days": days, "coverage": 0.0, "status": "no_data", "trade_date": trade_date}
        value = dict(row)
        value["trading_days"] = days
        value["platforms"] = json.loads(value.pop("platforms_json"))
        value["funnel"] = json.loads(value.pop("funnel_json"))
        return value

    def collection_dates(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT trade_date FROM collection_runs ORDER BY trade_date").fetchall()
        return [row["trade_date"] for row in rows]

    def validation_rows(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM outcomes ORDER BY signal_date").fetchall()
        return [dict(row) for row in rows]


def _decode_score(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["platforms"] = json.loads(value.pop("platforms_json"))
    value["platform_contributions"] = json.loads(value.pop("platform_contributions_json"))
    value["evidence"] = json.loads(value.pop("evidence_json"))
    return value


def _decode_creator(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["sector_ids"] = json.loads(value.pop("sector_ids_json"))
    value["evidence"] = json.loads(value.pop("evidence_json"))
    return value


def _bool_int(value: bool | None) -> int | None:
    return None if value is None else int(bool(value))


def _coverage_ok(score: dict) -> bool:
    return (len(score["platforms"]) >= MIN_PLATFORMS
            and score["independent_authors"] >= MIN_AUTHORS
            and score["mapping_confidence"] >= MIN_MAPPING_CONFIDENCE)


def build_daily_score(*, trade_date: str, sector_id: str, sector_name: str,
                      taxonomy: str, evidence: list[dict], history: list[dict],
                      market: dict) -> dict:
    """Aggregate each platform first, then combine platforms equally."""
    grouped: dict[str, list[dict]] = {}
    for row in evidence:
        grouped.setdefault(row["platform"], []).append(row)
    contributions = {}
    for platform, rows in grouped.items():
        weights = [max(0.05, float(row.get("confidence", 0))) * float(row.get("repost_weight", 1.0))
                   for row in rows]
        total = sum(weights) or 1.0
        net = sum(float(row.get("stance", 0)) * weight for row, weight in zip(rows, weights)) / total
        contributions[platform] = {
            "records": len(rows),
            "net_sentiment": net,
            "fomo": sum(float(row.get("fomo", 0)) * weight for row, weight in zip(rows, weights)) / total,
            "panic": sum(float(row.get("panic", 0)) * weight for row, weight in zip(rows, weights)) / total,
        }
    platform_values = list(contributions.values())
    net_sentiment = (sum(row["net_sentiment"] for row in platform_values) / len(platform_values)
                     if platform_values else 0.0)
    fomo = sum(row["fomo"] for row in platform_values) / len(platform_values) if platform_values else 0.0
    normalized_texts = [_fingerprint(str(row.get("text", ""))) for row in evidence]
    diversity = len(set(normalized_texts)) / len(normalized_texts) if normalized_texts else 1.0
    bullish_consensus = min(1.0, 0.7 * abs(net_sentiment) + 0.3 * (1 - diversity))
    content_count = sum(float(row.get("repost_weight", 1.0)) for row in evidence if not row.get("comment_id"))
    comment_count = sum(float(row.get("repost_weight", 1.0)) for row in evidence if row.get("comment_id"))
    engagement_raw = sum((1 + float(row.get("engagement", 0))) ** 0.5
                         * float(row.get("repost_weight", 1.0)) for row in evidence)
    author_count = len({row.get("author_hash") for row in evidence if row.get("author_hash")})
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
        "evidence": [{"platform": row["platform"], "text": str(row.get("text", ""))[:160],
                      "stance": row.get("stance", 0)} for row in evidence[:8]],
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
        if commit:
            actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                    text=True, check=True, timeout=10).stdout.strip()
            if actual != commit:
                raise RuntimeError(f"MediaCrawler commit mismatch: expected {commit}, got {actual}")
        before = {path: path.stat().st_mtime_ns for path in root.rglob("*.jsonl")}
        cmd = [str(root / ".venv" / "bin" / "python"), "main.py", "--platform", job.platform,
               "--type", job.mode]
        if job.mode == "search":
            cmd.extend(["--keywords", job.value])
        else:
            cmd.extend([creator_id_argument, job.value])
        subprocess.run(cmd, cwd=root, check=True, timeout=timeout)
        candidates = [path for path in root.rglob("*.jsonl")
                      if path.stat().st_mtime_ns > before.get(path, -1)]
        if not candidates:
            raise RuntimeError("collector finished without new JSONL output")
        normalized_rows = []
        for source in sorted(candidates):
            for line in source.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                normalized = normalize_external_record(json.loads(line), job.platform)
                if normalized:
                    normalized.update(
                        retrieval_source=job.mode,
                        retrieval_source_id=job.source_id,
                        sector_ids=list(job.sector_ids),
                    )
                    normalized_rows.append(normalized)

        max_contents = max(0, int(limits.get("max_contents", 20)))
        max_comments = max(0, int(limits.get("max_comments", 50)))
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


def normalize_external_record(raw: dict, platform: str) -> dict | None:
    """Map common MediaCrawler content/comment fields into Apex's canonical contract."""
    content_id = next((raw.get(key) for key in
                       ("content_id", "aweme_id", "video_id", "note_id", "id") if raw.get(key)), None)
    comment_id = next((raw.get(key) for key in ("comment_id", "cid") if raw.get(key)), "")
    text = next((raw.get(key) for key in
                 ("text", "content", "comment_content", "title", "desc") if raw.get(key)), "")
    if not content_id or not text:
        return None
    author = next((raw.get(key) for key in ("user_id", "author_id", "uid") if raw.get(key)), None)
    return {
        "platform": platform, "content_id": str(content_id), "comment_id": str(comment_id),
        "published_at": raw.get("published_at") or raw.get("create_time"),
        "collected_at": _now(), "text": str(text),
        "title": raw.get("title") or "",
        "description": raw.get("description") or raw.get("desc") or "",
        "tags": raw.get("tags") or [],
        "engagement": float(raw.get("engagement") or raw.get("like_count") or raw.get("liked_count") or 0),
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
                 empty_coverage: float = 1.0) -> dict:
    """Run typed retrieval jobs independently and retain every successful output."""
    base = Path(raw_dir) / trade_date
    base.mkdir(parents=True, exist_ok=True)
    statuses = []
    for job in jobs:
        destination = base / f"{job.platform}-{job.mode}-{uuid.uuid4().hex}.jsonl"
        status = {
            "platform": job.platform, "mode": job.mode, "value": job.value,
            "source_id": job.source_id, "sector_ids": list(job.sector_ids),
        }
        try:
            runner(job, destination, timeout, limits)
            count = sum(1 for line in destination.read_text(encoding="utf-8").splitlines()
                        if line.strip())
            statuses.append({**status, "status": "ok", "records": count,
                             "path": str(destination)})
        except Exception as exc:
            statuses.append({**status, "status": "failed", "records": 0,
                             "error": f"{type(exc).__name__}: {exc}"})
    successful = sum(item["status"] == "ok" for item in statuses)
    return {
        "trade_date": trade_date,
        "jobs": statuses,
        "coverage": successful / len(jobs) if jobs else float(empty_coverage),
        "as_of": _now(),
    }


def _ingest_and_classify_relevance(report: dict, store: SentimentStore,
                                   taxonomy: list[dict]) -> dict:
    """Ingest successful job outputs, then persist finance relevance decisions."""
    sector_terms = list(dict.fromkeys(
        term for sector in taxonomy
        for term in [sector.get("sector_name", ""), *(sector.get("aliases") or [])]
        if term
    ))
    totals = {"inserted": 0, "duplicates": 0}
    raw_recalled = accepted = 0
    source_ids: set[str] = set()
    for item in report["jobs"]:
        if item["status"] != "ok":
            continue
        try:
            path = Path(item["path"])
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            decisions = []
            for record in records:
                missing = [field for field in ("platform", "content_id", "text")
                           if not record.get(field)]
                if missing:
                    raise ValueError(f"canonical record missing: {', '.join(missing)}")
                record["retrieval_source"] = item["mode"]
                record["retrieval_source_id"] = item["source_id"]
                record["sector_ids"] = item["sector_ids"]
                decisions.append(classify_financial_relevance(
                    record, sector_terms, item["mode"] == "creator"))
            ingested = store.ingest(records, decisions)
        except Exception as exc:
            item.update(
                status="failed", records=0,
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        totals = {key: totals[key] + ingested[key] for key in totals}
        raw_recalled += len(records)
        accepted += sum(decision.decision == "accepted" for decision in decisions)
        if records:
            source_ids.add(item["source_id"])
    successful = sum(item["status"] == "ok" for item in report["jobs"])
    if report["jobs"]:
        report["coverage"] = successful / len(report["jobs"])
    return {
        **totals,
        "raw_recalled": raw_recalled,
        "financial_relevant": accepted,
        "sources": len(source_ids),
    }


def briefing_changes(store: SentimentStore, trade_date: str, coverage: float = 1.0) -> list[str]:
    labels = {"opened": "进入观察", "upgraded": "升级警戒", "resolved": "解除预警"}
    lines = [f'{event["sector_name"]}：{labels[event["event_type"]]}'
             for event in store.list_events() if event["trade_date"] == trade_date]
    if coverage < 1.0:
        lines.append(f"情绪数据覆盖不足（{coverage:.0%}），缺失平台不视为低风险")
    return lines


def _semantic_evidence(record: dict, taxonomy: list[dict]) -> list[dict]:
    text = str(record.get("text", ""))
    bullish = ("看多", "起飞", "上车", "加仓", "坚定", "突破", "牛市")
    bearish = ("看空", "跑路", "清仓", "见顶", "暴跌", "退潮", "割肉")
    fomo_words = ("必须", "赶紧", "梭哈", "错过", "上车", "起飞")
    panic_words = ("快跑", "割肉", "崩盘", "清仓", "暴跌")
    stance = 1.0 if any(word in text for word in bullish) else -1.0 if any(word in text for word in bearish) else 0.0
    confidence = 0.9 if stance else 0.5
    output = []
    for sector in taxonomy:
        aliases = sector.get("aliases") or [sector.get("sector_name", "")]
        if not any(alias and alias in text for alias in aliases):
            continue
        output.append({
            "platform": record["platform"], "content_id": record["content_id"],
            "comment_id": record.get("comment_id", ""), "author_hash": record.get("author_hash"),
            "text": text, "engagement": record.get("engagement", 0),
            "sector_id": sector["sector_id"], "sector_name": sector["sector_name"],
            "taxonomy": sector["taxonomy"], "stance": stance, "confidence": confidence,
            "fomo": float(any(word in text for word in fomo_words)),
            "panic": float(any(word in text for word in panic_words)),
            "mapping_confidence": 0.95, "narrative": next((a for a in aliases if a in text), ""),
            "evidence_span": text[:160],
            "repost_weight": float(record.get("repost_weight", 1.0)),
        })
    return output


def llm_semantic_evidence(record: dict, taxonomy: list[dict]) -> list[dict]:
    """Classify a low-confidence text into the fixed evidence contract."""
    from apex import llm
    allowed = [{"sector_id": item["sector_id"], "sector_name": item["sector_name"],
                "taxonomy": item["taxonomy"], "aliases": item.get("aliases", [])}
               for item in taxonomy]
    prompt = (
        "只返回JSON数组。对文本做板块映射与散户情绪分类。每项字段必须为: "
        "sector_id, stance(-1到1), confidence(0到1), fomo(0到1), panic(0到1), "
        "narrative, evidence_span。sector_id只能来自候选。\n"
        f"候选={json.dumps(allowed, ensure_ascii=False)}\n文本={record.get('text','')}"
    )
    reply = llm.chat([{"role": "system", "content": f"prompt_version={PROMPT_VERSION}"},
                      {"role": "user", "content": prompt}], temperature=0, max_tokens=1200)
    content = reply["content"].strip().removeprefix("```json").removesuffix("```").strip()
    values = json.loads(content)
    by_id = {item["sector_id"]: item for item in taxonomy}
    output = []
    for value in values if isinstance(values, list) else []:
        meta = by_id.get(value.get("sector_id"))
        if not meta:
            continue
        output.append({
            **value, "platform": record["platform"], "content_id": record["content_id"],
            "comment_id": record.get("comment_id", ""), "author_hash": record.get("author_hash"),
            "text": record.get("text", ""), "engagement": record.get("engagement", 0),
            "sector_name": meta["sector_name"], "taxonomy": meta["taxonomy"],
            "mapping_confidence": float(value.get("confidence", 0)),
            "repost_weight": float(record.get("repost_weight", 1.0)),
        })
    return output


def run_configured(cfg: dict | None = None, *, runner: Callable | None = None,
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
    retrieval_settings = dict(settings.get("retrieval") or {})
    timeout = int(settings.get("timeout_seconds", 900))
    limits = {
        "max_contents": int(retrieval_settings.get("max_contents_per_query", 20)),
        "max_comments": int(retrieval_settings.get("max_comments_per_content", 50)),
    }
    if runner is None:
        external_root = Path(str(settings["mediacrawler_path"])).expanduser()
        runner = default_mediacrawler_runner(
            external_root,
            settings.get("mediacrawler_commit"),
            retrieval_settings.get("creator_id_argument", "--creator_id"),
        )
    resolved_date = trade_date or date.today().isoformat()
    raw_dir = cache_dir / "raw"
    store = open_store(cache_dir)

    search_jobs = build_search_jobs(taxonomy, platforms, retrieval_settings)
    search_report = collect_jobs(
        search_jobs, runner, raw_dir, resolved_date, limits, timeout,
        empty_coverage=0.0,
    )
    search_ingest = _ingest_and_classify_relevance(search_report, store, taxonomy)
    store.discover_creator_candidates(store.accepted_search_records(resolved_date), retrieval_settings)
    creator_jobs = build_creator_jobs(store.approved_creators(), platforms)
    creator_report = collect_jobs(
        creator_jobs, runner, raw_dir, resolved_date, limits, timeout,
    )
    creator_ingest = _ingest_and_classify_relevance(creator_report, store, taxonomy)
    for item in creator_report["jobs"]:
        store.set_creator_collection_error(
            item["platform"], item["value"], item.get("error") if item["status"] == "failed" else None,
        )
    totals = {
        key: search_ingest[key] + creator_ingest[key]
        for key in ("inserted", "duplicates")
    }
    funnel = {
        "raw_recalled": search_ingest["raw_recalled"] + creator_ingest["raw_recalled"],
        "financial_relevant": (
            search_ingest["financial_relevant"] + creator_ingest["financial_relevant"]
        ),
        "filtered": 0,
        "search_sources": search_ingest["sources"],
        "creator_sources": creator_ingest["sources"],
    }
    funnel["filtered"] = funnel["raw_recalled"] - funnel["financial_relevant"]
    all_jobs = search_report["jobs"] + creator_report["jobs"]
    successful_jobs = sum(item["status"] == "ok" for item in all_jobs)
    overall_coverage = (
        successful_jobs / len(all_jobs) if all_jobs and search_jobs else 0.0
    )
    report = {
        "trade_date": resolved_date,
        "search_coverage": search_report["coverage"],
        "creator_coverage": creator_report["coverage"],
        "coverage": overall_coverage,
        "jobs": all_jobs,
        "search_jobs": search_report["jobs"],
        "creator_jobs": creator_report["jobs"],
        "platforms": {},
        "funnel": funnel,
        "as_of": _now(),
    }
    store.record_collection(report)
    all_records = store.list_eligible_content(resolved_date)
    if semantic_classifier is None:
        if settings.get("llm_enabled", False):
            def semantic_classifier(record, taxonomy):
                baseline = _semantic_evidence(record, taxonomy)
                if baseline and all(item["stance"] != 0 for item in baseline):
                    return baseline
                return llm_semantic_evidence(record, taxonomy)
        else:
            semantic_classifier = _semantic_evidence
    evidence = []
    classification_errors = 0
    for record in all_records:
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
        score = build_daily_score(
            trade_date=resolved_date, sector_id=sector_id, sector_name=meta["sector_name"],
            taxonomy=meta["taxonomy"], evidence=rows, history=history,
            market=market,
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
