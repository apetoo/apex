from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


_CANONICAL_DETAIL_PATH = re.compile(r"/news,([A-Za-z0-9_-]+),([A-Za-z0-9_-]+)\.html\Z")


def _is_canonical_detail_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.netloc == "guba.eastmoney.com"
        and not parsed.query
        and not parsed.fragment
        and _CANONICAL_DETAIL_PATH.fullmatch(parsed.path)
    )


class EastmoneyExporter:
    def __init__(self, output: Path, *, author_salt: str):
        if not author_salt:
            raise ValueError("author_salt is required")
        self.output = Path(output)
        self.author_salt = author_salt
        self._seen: set[tuple] = set()
        if self.output.exists():
            for line in self.output.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                    self._seen.add((item.get("batch_id"), item.get("content_id"),
                                    item.get("comment_id", ""), tuple(sorted(item.get("sector_ids", [])))))
                except (json.JSONDecodeError, TypeError):
                    continue

    def export(self, records: list[dict], *, trade_date: str, batch_id: str, sector_id: str,
               source_type: str, stock_code: str | None, pool_version: str,
               collected_at: str, sector_ids: list[str] | None = None,
               target_mappings: list[dict] | None = None) -> int:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        sectors = sorted(set(sector_ids or [sector_id]))
        with self.output.open("a", encoding="utf-8") as stream:
            for item in records:
                source_url = str(item.get("url") or "")
                if not _is_canonical_detail_url(source_url):
                    raise ValueError("record requires a canonical Eastmoney detail URL")
                identity = (batch_id, str(item["content_id"]), str(item.get("comment_id") or ""),
                            tuple(sectors))
                if identity in self._seen:
                    continue
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
                    "url": source_url,
                    "author_hash": hashlib.sha256(
                        f"{self.author_salt}:{author}".encode("utf-8")
                    ).hexdigest() if author else None,
                    "source_type": source_type, "retrieval_source": "eastmoney_guba",
                    "retrieval_source_id": f"{source_type}:{stock_code or sector_id}",
                    "sector_ids": sectors, "stock_code": stock_code,
                    "target_mappings": target_mappings or [],
                    "target_pool_version": pool_version,
                }
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                self._seen.add(identity)
                written += 1
        return written
