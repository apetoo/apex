from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CollectionStatus = Literal[
    "ok", "partial", "empty_valid", "schema_changed", "blocked", "failed"
]


@dataclass(frozen=True)
class ForumTarget:
    sector_id: str
    forum_id: str
    source_type: Literal["sector_forum", "constituent_forum"]
    stock_code: str | None
    pool_version: str
    generated_at: str


@dataclass(frozen=True)
class CollectionResult:
    status: CollectionStatus
    records: int
    request_count: int
    failed_targets: int
    quota_exhausted: bool = False
    message: str | None = None


class QuotaBudget:
    def __init__(self, max_requests: int):
        if isinstance(max_requests, bool) or max_requests < 0:
            raise ValueError("max_requests must be a non-negative integer")
        self.max_requests = int(max_requests)
        self.request_count = 0

    def consume(self) -> bool:
        if self.request_count >= self.max_requests:
            return False
        self.request_count += 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.request_count >= self.max_requests
