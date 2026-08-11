"""Versioned, finance-anchored query planning for sector sentiment retrieval."""

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Literal


QUERY_VERSION = "sector-finance-query-v1"
DEFAULT_QUERY_TEMPLATES = (
    "{term} 股票", "{term} A股", "{term} 板块", "{term} 概念股",
    "{term} ETF", "{term} 龙头", "{term} 投资", "{term} 行情",
)
FINANCIAL_ANCHORS = ("股票", "A股", "板块", "概念股", "ETF", "龙头", "投资", "行情")
RELEVANCE_VERSION = "sector-finance-relevance-v1"
CREATOR_RULE_VERSION = "sector-finance-creator-v1"
FINANCE_TERMS = ("股票", "A股", "板块", "概念股", "ETF", "行情", "主力", "资金", "涨停", "估值", "持仓", "加仓", "减仓")
EXCLUDE_TERMS = ("教程", "编程", "机械臂安装", "产品测评", "比赛", "玩具")


@dataclass(frozen=True)
class RetrievalJob:
    platform: str
    mode: Literal["search", "creator"]
    value: str
    sector_ids: tuple[str, ...]
    source_id: str
    trade_date: str | None = None
    cutoff: str | None = None
    config_hash: str | None = None
    crawl_locator: str | None = None

    @property
    def budget_key(self) -> str:
        """Return a non-identifying, deterministic key for a dated job budget."""
        payload = json.dumps(
            [self.platform, self.mode, self.value],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RelevanceDecision:
    score: float
    decision: Literal["accepted", "review", "filtered_non_financial"]
    reasons: tuple[str, ...]
    version: str

    def __post_init__(self) -> None:
        if self.decision not in {"accepted", "review", "filtered_non_financial"}:
            raise ValueError("invalid financial relevance decision")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ValueError("financial relevance score must be numeric")
        if not math.isfinite(float(self.score)) or not 0 <= float(self.score) <= 1:
            raise ValueError("financial relevance score must be finite and in [0,1]")
        if not self.version:
            raise ValueError("financial relevance version is required")
        if not self.reasons or any(not isinstance(reason, str) or not reason for reason in self.reasons):
            raise ValueError("financial relevance decisions require audit reasons")


def classify_financial_relevance(
    record: dict, sector_terms: list[str], approved_author: bool,
) -> RelevanceDecision:
    """Score whether a retrieved record is relevant to financial sentiment."""
    headline = " ".join(str(record.get(key) or "") for key in ("title", "description", "tags"))
    body = str(record.get("text") or "")
    entity_headline = any(term in headline for term in sector_terms)
    entity_body = any(term in body for term in sector_terms)
    finance_head = [term for term in FINANCE_TERMS if term in headline]
    finance_body = [term for term in FINANCE_TERMS if term in body]
    excludes = [term for term in EXCLUDE_TERMS if term in headline or term in body]
    finance = bool(finance_head or finance_body)
    evidence = [*(f"finance:{term}" for term in dict.fromkeys(finance_head + finance_body)),
                *(f"exclude:{term}" for term in dict.fromkeys(excludes))]

    if entity_headline and finance and not excludes:
        return RelevanceDecision(
            0.90, "accepted", tuple(["path:entity_and_finance", *evidence]),
            RELEVANCE_VERSION,
        )
    if approved_author and finance and not excludes:
        return RelevanceDecision(
            0.80, "accepted", tuple(["path:approved_author_investment", *evidence]),
            RELEVANCE_VERSION,
        )
    if excludes:
        return RelevanceDecision(
            0.10, "filtered_non_financial",
            tuple([*evidence, "audit:excluded_non_financial_context"]),
            RELEVANCE_VERSION,
        )
    if entity_headline or entity_body or finance:
        return RelevanceDecision(
            0.50 if (entity_headline or entity_body) and finance else 0.45,
            "review",
            tuple([*evidence, "audit:low_confidence_requires_llm"]),
            RELEVANCE_VERSION,
        )
    return RelevanceDecision(
        0.0, "filtered_non_financial",
        ("audit:no_sector_or_finance_match",), RELEVANCE_VERSION,
    )


def build_search_jobs(
    taxonomy: list[dict], platforms: list[str], settings: dict, *,
    trade_date: str | None = None, config_hash: str | None = None,
) -> list[RetrievalJob]:
    """Build deterministic, finance-anchored search jobs for a frozen taxonomy."""
    templates = tuple(settings.get("query_templates") or DEFAULT_QUERY_TEMPLATES)
    limit = int(settings.get("max_queries_per_sector", 8))
    if limit < 0:
        raise ValueError("max_queries_per_sector must be non-negative")
    planned: dict[tuple[str, str], list[str]] = {}

    for template in templates:
        if not isinstance(template, str) or "{term}" not in template:
            raise ValueError(f"query template must contain {{term}}: {template!r}")

    for sector in taxonomy:
        terms = tuple(dict.fromkeys(
            term.strip() for term in [sector.get("sector_name"), *sector.get("aliases", [])]
            if isinstance(term, str) and term.strip()
        ))
        if not terms:
            raise ValueError(
                f"sector {sector.get('sector_id')!r} requires a non-empty taxonomy term"
            )
        queries = []
        for term in terms:
            for template in templates:
                query = template.format(term=term).strip()
                remainder = query.replace(str(term), "", 1)
                if str(term) not in query or not any(anchor in remainder for anchor in FINANCIAL_ANCHORS):
                    raise ValueError(
                        f"query template rendered without a financial anchor: {template!r}"
                    )
                if query not in queries:
                    queries.append(query)
        queries = queries[:limit]
        for platform in platforms:
            for query in queries:
                key = (platform, query)
                sector_ids = planned.setdefault(key, [])
                if sector["sector_id"] not in sector_ids:
                    sector_ids.append(sector["sector_id"])
    return [
        RetrievalJob(
            platform=platform, mode="search", value=query,
            sector_ids=tuple(sector_ids), source_id=f"query:{query}",
            trade_date=trade_date, config_hash=config_hash,
        )
        for (platform, query), sector_ids in planned.items()
    ]


def build_creator_jobs(
    creators: list[dict], platforms: list[str], *, trade_date: str | None = None,
    config_hash: str | None = None,
) -> list[RetrievalJob]:
    """Build deterministic approved-creator jobs scoped to enabled platforms."""
    enabled_platforms = set(platforms)
    jobs = []
    for creator in sorted(creators, key=lambda value: (value["platform"], value["creator_id"])):
        cutoff = creator.get("approved_at")
        if (creator.get("status") != "approved" or creator["platform"] not in enabled_platforms
                or not cutoff):
            continue
        creator_id = str(creator["creator_id"])
        jobs.append(RetrievalJob(
            platform=creator["platform"], mode="creator", value=creator_id,
            sector_ids=tuple(sorted(set(creator.get("sector_ids", [])))),
            source_id=f"creator:{creator['platform']}:{creator_id}",
            trade_date=trade_date, cutoff=str(cutoff), config_hash=config_hash,
            crawl_locator=str(creator.get("crawl_locator") or "") or None,
        ))
    return jobs
