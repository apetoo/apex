from __future__ import annotations

from .models import ForumTarget


class EastmoneyTargetProvider:
    """Create deterministic targets from explicit forum IDs and turnover-ranked stocks."""

    def __init__(self, constituent_count: int = 5, pool_version: str = "eastmoney-target-v1"):
        if constituent_count < 0:
            raise ValueError("constituent_count must be non-negative")
        if not pool_version:
            raise ValueError("pool_version is required")
        self.constituent_count = constituent_count
        self.pool_version = pool_version

    def build_targets(self, taxonomy: list[dict], constituents: dict[str, list[dict]], *, generated_at: str):
        targets: list[ForumTarget] = []
        missing: list[dict[str, str]] = []
        for sector in taxonomy:
            sector_id = str(sector["sector_id"])
            forum_id = str(sector.get("eastmoney_forum_id") or "").strip()
            if forum_id:
                targets.append(ForumTarget(
                    sector_id, forum_id, "sector_forum", None, self.pool_version, generated_at
                ))
            else:
                missing.append({"sector_id": sector_id, "reason": "missing_explicit_forum_id"})
            ranked = sorted(
                constituents.get(sector_id, []),
                key=lambda row: (-float(row.get("turnover_20d") or 0), str(row.get("stock_code") or "")),
            )
            for row in ranked[: self.constituent_count]:
                code = str(row.get("stock_code") or "").strip()
                if code:
                    targets.append(ForumTarget(
                        sector_id, code, "constituent_forum", code,
                        self.pool_version, generated_at,
                    ))
        return targets, missing
