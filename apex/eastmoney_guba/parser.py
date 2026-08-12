from __future__ import annotations

import json
from html.parser import HTMLParser


class BlockedResponse(ValueError):
    pass


class SchemaChanged(ValueError):
    pass


_BLOCK_MARKERS = ("验证码", "访问过于频繁", "请登录", "安全验证")


class _PostHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.posts: list[dict] = []
        self.current: dict | None = None
        self.field: str | None = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = set(values.get("class", "").split())
        if "articleh" in classes and values.get("data-post-id"):
            self.current = {
                "content_id": values["data-post-id"], "author_id": values.get("data-user-id"),
                "title": "", "text": "", "published_at": None,
                "read_count": 0, "reply_count": 0, "like_count": 0,
            }
            self.posts.append(self.current)
        if self.current is not None:
            if "l3" in classes:
                self.field = "title"
                self.current["url"] = values.get("href")
            elif "publish_time" in classes:
                self.field = "published_at"
            elif "read" in classes:
                self.field = "read_count"
            elif "reply" in classes:
                self.field = "reply_count"

    def handle_endtag(self, tag):
        self.field = None

    def handle_data(self, data):
        if self.current is None or self.field is None:
            return
        value = data.strip()
        if not value:
            return
        self.current[self.field] = int(value) if self.field.endswith("_count") else value


class _CommentHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.comments: list[dict] = []
        self.current: dict | None = None
        self.field: str | None = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = set(values.get("class", "").split())
        if "comment_item" in classes and values.get("data-comment-id") and values.get("data-post-id"):
            post_id = values["data-post-id"]
            self.current = {
                "content_id": post_id, "comment_id": values["data-comment-id"],
                "parent_content_id": post_id, "title": "", "text": "",
                "published_at": None, "author_id": values.get("data-user-id"),
                "read_count": 0, "reply_count": 0, "like_count": 0,
            }
            self.comments.append(self.current)
        if self.current is not None:
            if "comment_content" in classes:
                self.field = "text"
            elif "comment_time" in classes:
                self.field = "published_at"
            elif "comment_like" in classes:
                self.field = "like_count"

    def handle_endtag(self, tag):
        self.field = None

    def handle_data(self, data):
        if self.current is None or self.field is None:
            return
        value = data.strip()
        if value:
            self.current[self.field] = int(value) if self.field == "like_count" else value


class EastmoneyParser:
    def parse_posts(self, body: bytes, content_type: str) -> list[dict]:
        text = body.decode("utf-8", errors="replace")
        self._ensure_not_blocked(text)
        if "json" in content_type.lower():
            payload = self._json(text)
            rows = payload.get("re")
            if not isinstance(rows, list):
                raise SchemaChanged("post response missing re list")
            return [self._post(row) for row in rows if not self._ignored_post(row)]
        parser = _PostHTMLParser()
        parser.feed(text)
        if not parser.posts:
            raise SchemaChanged("post HTML contains no recognized rows")
        return parser.posts

    def parse_comments(self, body: bytes, content_type: str) -> list[dict]:
        text = body.decode("utf-8", errors="replace")
        self._ensure_not_blocked(text)
        if "json" not in content_type.lower():
            parser = _CommentHTMLParser()
            parser.feed(text)
            if not parser.comments:
                raise SchemaChanged("comment HTML contains no recognized rows")
            return parser.comments
        payload = self._json(text)
        rows = payload.get("re")
        if not isinstance(rows, list):
            raise SchemaChanged("comment response missing re list")
        return [self._comment(row) for row in rows if not row.get("reply_to_comment_id")]

    @staticmethod
    def _ensure_not_blocked(text: str) -> None:
        if any(marker in text for marker in _BLOCK_MARKERS):
            raise BlockedResponse("Eastmoney returned an access-control page")

    @staticmethod
    def _json(text: str) -> dict:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SchemaChanged("invalid JSON response") from exc
        if not isinstance(value, dict):
            raise SchemaChanged("JSON response must be an object")
        return value

    @staticmethod
    def _ignored_post(row: dict) -> bool:
        return bool(row.get("is_ad") or row.get("is_notice") or row.get("post_type") in {1, 2})

    @staticmethod
    def _post(row: dict) -> dict:
        required = ("post_id", "post_publish_time")
        if any(not row.get(key) for key in required):
            raise SchemaChanged("post row missing identity or time")
        return {
            "content_id": str(row["post_id"]), "comment_id": "",
            "title": str(row.get("post_title") or ""), "text": str(row.get("post_content") or ""),
            "published_at": row["post_publish_time"], "author_id": row.get("user_id"),
            "read_count": int(row.get("post_click_count") or 0),
            "reply_count": int(row.get("post_comment_count") or 0),
            "like_count": int(row.get("post_like_count") or 0),
        }

    @staticmethod
    def _comment(row: dict) -> dict:
        if not row.get("comment_id") or not row.get("post_id") or not row.get("comment_publish_time"):
            raise SchemaChanged("comment row missing identity, parent, or time")
        return {
            "content_id": str(row["post_id"]), "comment_id": str(row["comment_id"]),
            "parent_content_id": str(row["post_id"]), "title": "",
            "text": str(row.get("comment_content") or ""),
            "published_at": row["comment_publish_time"], "author_id": row.get("user_id"),
            "read_count": 0, "reply_count": 0,
            "like_count": int(row.get("comment_like_count") or 0),
        }
