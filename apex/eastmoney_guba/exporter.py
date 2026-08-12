from __future__ import annotations

import hashlib
import json
from pathlib import Path


class EastmoneyExporter:
    def __init__(self, output: Path, *, author_salt: str):
        if not author_salt:
            raise ValueError("author_salt is required")
        self.output = Path(output)
        self.author_salt = author_salt

    def export(self, records: list[dict], *, trade_date: str, batch_id: str, sector_id: str,
               source_type: str, stock_code: str | None, pool_version: str,
               collected_at: str) -> int:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with self.output.open("a", encoding="utf-8") as stream:
            for item in records:
                author = str(item.get("author_id") or "")
                record = {
                    "platform": "eastmoney", "content_id": str(item["content_id"]),
                    "comment_id": str(item.get("comment_id") or ""),
                    "parent_content_id": item.get("parent_content_id"),
                    "published_at": item.get("published_at"), "collected_at": collected_at,
                    "trade_date": trade_date, "batch_id": batch_id,
                    "title": item.get("title") or "", "text": item.get("text") or "",
                    "engagement": sum(int(item.get(key) or 0) for key in
                                      ("read_count", "reply_count", "like_count")),
                    "url": item.get("url"),
                    "author_hash": hashlib.sha256(
                        f"{self.author_salt}:{author}".encode("utf-8")
                    ).hexdigest() if author else None,
                    "source_type": source_type, "retrieval_source": "eastmoney_guba",
                    "retrieval_source_id": f"{source_type}:{stock_code or sector_id}",
                    "sector_ids": [sector_id], "stock_code": stock_code,
                    "target_pool_version": pool_version,
                }
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return len(records)
