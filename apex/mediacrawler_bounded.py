"""Run a pinned MediaCrawler creator job with process-local content bounds.

The pinned MediaCrawler CLI accepts the common note limit, but its Bilibili and
Douyin creator paths do not consult it while paging.  This entrypoint patches
only those two in-process methods before starting upstream; no checkout files
or shared configuration are mutated, and process exit restores the boundary.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


ADAPTER_VERSION = "apex-mediacrawler-bounded-v1"


def install_creator_bounds(
    upstream_main: Any, config_module: Any, douyin_client_class: type, *,
    creator_locator: str | None = None,
    approved_creator_hash: str | None = None,
    anonymize_user_id: Any = None,
) -> None:
    """Install deterministic creator pagination limits on supported platforms."""
    bili_crawler_class = upstream_main.BilibiliCrawler
    original_douyin_user_info = douyin_client_class.get_user_info

    def verify_creator(raw_user_id: Any) -> None:
        if not creator_locator:
            return
        if not approved_creator_hash or anonymize_user_id is None:
            raise RuntimeError("creator locator requires an approved anonymous identity")
        if anonymize_user_id(raw_user_id) != approved_creator_hash:
            raise RuntimeError("creator locator does not match the approved anonymous identity")

    async def bounded_bili_creator_videos(self: Any, creator_id: int) -> None:
        limit = max(0, int(config_module.CRAWLER_MAX_NOTES_COUNT))
        if limit == 0:
            return
        if creator_locator:
            try:
                detail = await self.bili_client.get_video_info(aid=int(creator_locator))
                raw_creator_id = detail["View"]["owner"]["mid"]
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("cannot resolve the approved Bilibili creator locator") from exc
            verify_creator(raw_creator_id)
            creator_id = int(raw_creator_id)
        page_size = min(30, limit)
        page = 1
        collected = 0
        while collected < limit:
            response = await self.bili_client.get_creator_videos(
                creator_id, page, page_size,
            )
            videos = list(response.get("list", {}).get("vlist") or [])
            remaining = limit - collected
            video_ids = [
                video["bvid"] for video in videos[:remaining]
                if isinstance(video, dict) and video.get("bvid")
            ]
            if video_ids:
                await self.get_specified_videos(video_ids)
            collected += len(video_ids)
            total = int(response.get("page", {}).get("count") or 0)
            if not videos or collected >= limit or total <= page * page_size:
                break
            page += 1

    async def resolve_douyin_creator(self: Any) -> tuple[str, dict]:
        cached = getattr(self, "_apex_resolved_creator", None)
        if cached is not None:
            return cached
        if not creator_locator:
            raise RuntimeError("Douyin creator locator is missing")
        detail = await self.get_video_by_id(creator_locator)
        author = detail.get("author") if isinstance(detail, dict) else None
        if not isinstance(author, dict):
            raise RuntimeError("cannot resolve the approved Douyin creator locator")
        raw_user_id = author.get("uid")
        sec_user_id = author.get("sec_uid") or author.get("sec_user_id")
        if not raw_user_id or not sec_user_id:
            raise RuntimeError("resolved Douyin creator identity is incomplete")
        verify_creator(raw_user_id)
        resolved = (str(sec_user_id), author)
        setattr(self, "_apex_resolved_creator", resolved)
        return resolved

    async def bounded_douyin_user_info(self: Any, sec_user_id: str) -> Any:
        if creator_locator and sec_user_id == creator_locator:
            _resolved_id, author = await resolve_douyin_creator(self)
            return author
        return await original_douyin_user_info(self, sec_user_id)

    async def bounded_douyin_creator_posts(
        self: Any, sec_user_id: str, callback: Any = None,
    ) -> list[dict]:
        limit = max(0, int(config_module.CRAWLER_MAX_NOTES_COUNT))
        if creator_locator:
            sec_user_id, _author = await resolve_douyin_creator(self)
        has_more = 1
        cursor = ""
        result: list[dict] = []
        while has_more == 1 and len(result) < limit:
            response = await self.get_user_aweme_posts(sec_user_id, cursor)
            has_more = int(response.get("has_more", 0) or 0)
            cursor = str(response.get("max_cursor") or "")
            rows = list(response.get("aweme_list") or [])
            rows = rows[:limit - len(result)]
            if callback and rows:
                await callback(rows)
            result.extend(rows)
            if not rows:
                break
        return result

    bili_crawler_class.get_creator_videos = bounded_bili_creator_videos
    douyin_client_class.get_user_info = bounded_douyin_user_info
    douyin_client_class.get_all_user_aweme_posts = bounded_douyin_creator_posts


def _load_upstream_main(root: Path) -> ModuleType:
    entrypoint = root / "main.py"
    if not entrypoint.is_file():
        raise FileNotFoundError(f"MediaCrawler not found: {root}")
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("_apex_pinned_mediacrawler", entrypoint)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load MediaCrawler entrypoint: {entrypoint}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    """Load the checkout at cwd, install bounds, and delegate its normal lifecycle."""
    root = Path.cwd().resolve()
    upstream = _load_upstream_main(root)
    import config as crawler_config
    from media_platform.douyin.client import DouYinClient
    from tools.app_runner import run
    from tools.user_hash import anonymize_user_id

    install_creator_bounds(
        upstream, crawler_config, DouYinClient,
        creator_locator=os.environ.get("APEX_CREATOR_CONTENT_ID"),
        approved_creator_hash=os.environ.get("APEX_APPROVED_CREATOR_HASH"),
        anonymize_user_id=anonymize_user_id,
    )
    run(
        upstream.main,
        upstream.async_cleanup,
        cleanup_timeout_seconds=15.0,
        on_first_interrupt=None,
    )


if __name__ == "__main__":
    main()
