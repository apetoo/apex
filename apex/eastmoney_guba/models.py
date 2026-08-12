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
    def __init__(self, max_requests: int, requests_per_target: int | None = None):
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 0:
            raise ValueError("max_requests must be a non-negative integer")
        self.max_requests = max_requests
        per_target = max_requests if requests_per_target is None else requests_per_target
        if isinstance(per_target, bool) or not isinstance(per_target, int) or per_target < 0:
            raise ValueError("requests_per_target must be a non-negative integer")
        self.requests_per_target = per_target
        self.request_count = 0
        self.target_requests: dict[str, int] = {}
        self.denied = False

    def consume(self, target_id: str = "default") -> bool:
        if (self.request_count >= self.max_requests
                or self.target_requests.get(target_id, 0) >= self.requests_per_target):
            self.denied = True
            return False
        self.request_count += 1
        self.target_requests[target_id] = self.target_requests.get(target_id, 0) + 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.denied or self.request_count >= self.max_requests
