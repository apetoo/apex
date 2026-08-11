"""Versioned, finance-anchored query planning for sector sentiment retrieval."""

from dataclasses import dataclass
from typing import Literal


QUERY_VERSION = "sector-finance-query-v1"
DEFAULT_QUERY_TEMPLATES = (
    "{term} 股票", "{term} A股", "{term} 板块", "{term} 概念股",
    "{term} ETF", "{term} 龙头", "{term} 投资", "{term} 行情",
)


@dataclass(frozen=True)
class RetrievalJob:
    platform: str
    mode: Literal["search", "creator"]
    value: str
    sector_ids: tuple[str, ...]
    source_id: str


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
        queries = list(dict.fromkeys(
            template.format(term=term).strip()
            for term in terms
            for template in templates
        ))[:limit]
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
