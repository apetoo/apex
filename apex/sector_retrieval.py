"""Versioned, finance-anchored query planning for sector sentiment retrieval."""

from dataclasses import dataclass
from typing import Literal


QUERY_VERSION = "sector-finance-query-v1"
DEFAULT_QUERY_TEMPLATES = (
    "{term} 股票", "{term} A股", "{term} 板块", "{term} 概念股",
    "{term} ETF", "{term} 龙头", "{term} 投资", "{term} 行情",
)
FINANCIAL_ANCHORS = ("股票", "A股", "板块", "概念股", "ETF", "龙头", "投资", "行情")
RELEVANCE_VERSION = "sector-finance-relevance-v1"
FINANCE_TERMS = ("股票", "A股", "板块", "概念股", "ETF", "行情", "主力", "资金", "涨停", "估值", "持仓", "加仓", "减仓")
EXCLUDE_TERMS = ("教程", "编程", "机械臂安装", "产品测评", "比赛", "玩具")


@dataclass(frozen=True)
class RetrievalJob:
    platform: str
    mode: Literal["search", "creator"]
    value: str
    sector_ids: tuple[str, ...]
    source_id: str


@dataclass(frozen=True)
class RelevanceDecision:
    score: float
    decision: Literal["accepted", "review", "filtered_non_financial"]
    reasons: tuple[str, ...]
    version: str


def classify_financial_relevance(
    record: dict, sector_terms: list[str], approved_author: bool,
) -> RelevanceDecision:
    """Score whether a retrieved record is relevant to financial sentiment."""
    headline = " ".join(str(record.get(key) or "") for key in ("title", "description", "tags"))
    body = str(record.get("text") or "")
    entity = any(term in headline or term in body for term in sector_terms)
    finance_head = [term for term in FINANCE_TERMS if term in headline]
    finance_body = [term for term in FINANCE_TERMS if term in body]
    excludes = [term for term in EXCLUDE_TERMS if term in headline or term in body]
    score = min(1.0, 0.35 * entity + 0.35 * bool(finance_head) +
                0.20 * bool(finance_body) + 0.15 * (approved_author and bool(finance_body)) -
                0.35 * bool(excludes))
    reasons = tuple([*(f"finance:{term}" for term in finance_head + finance_body),
                     *(f"exclude:{term}" for term in excludes)])
    decision = "accepted" if score >= 0.7 else "review" if score >= 0.4 else "filtered_non_financial"
    return RelevanceDecision(max(0.0, score), decision, reasons, RELEVANCE_VERSION)


def build_search_jobs(
    taxonomy: list[dict], platforms: list[str], settings: dict,
) -> list[RetrievalJob]:
    """Build deterministic, finance-anchored search jobs for a frozen taxonomy."""
    templates = tuple(settings.get("query_templates") or DEFAULT_QUERY_TEMPLATES)
    limit = int(settings.get("max_queries_per_sector", 8))
    jobs: list[RetrievalJob] = []
    seen: set[tuple[str, str]] = set()

    for sector in taxonomy:
        terms = dict.fromkeys([sector["sector_name"], *sector.get("aliases", [])])
        queries = []
        for term in terms:
            for template in templates:
                query = template.format(term=term).strip()
                if not any(anchor in query for anchor in FINANCIAL_ANCHORS):
                    raise ValueError(
                        f"query template rendered without a financial anchor: {template!r}"
                    )
                if query not in queries:
                    queries.append(query)
        queries = queries[:limit]
        for platform in platforms:
            for query in queries:
                key = (platform, query)
                if key not in seen:
                    seen.add(key)
                    jobs.append(RetrievalJob(
                        platform=platform,
                        mode="search",
                        value=query,
                        sector_ids=(sector["sector_id"],),
                        source_id=f"query:{query}",
                    ))
    return jobs


def build_creator_jobs(creators: list[dict], platforms: list[str]) -> list[RetrievalJob]:
    """Build deterministic approved-creator jobs scoped to enabled platforms."""
    enabled_platforms = set(platforms)
    jobs = []
    for creator in sorted(creators, key=lambda value: (value["platform"], value["creator_id"])):
        if creator.get("status") != "approved" or creator["platform"] not in enabled_platforms:
            continue
        creator_id = str(creator["creator_id"])
        jobs.append(RetrievalJob(
            platform=creator["platform"], mode="creator", value=creator_id,
            sector_ids=tuple(sorted(set(creator.get("sector_ids", [])))),
            source_id=f"creator:{creator['platform']}:{creator_id}",
        ))
    return jobs
