from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser


class BlockedResponse(ValueError):
    pass


class SchemaChanged(ValueError):
    pass


_BLOCK_MARKERS = ("验证码", "访问过于频繁", "请登录", "安全验证")


@dataclass(frozen=True)
class CommentPage:
    records: list[dict]
    has_more: bool
    next_cursor: str | None
    window_exhausted: bool = False


def _count(value) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        raise SchemaChanged("numeric count cannot be boolean")
    if isinstance(value, (int, float)):
        if value < 0:
            raise SchemaChanged("numeric count cannot be negative")
        return int(value)
    text = str(value).strip().replace(",", "")
    multiplier = 10000 if text.endswith("万") else 1
    if multiplier != 1:
        text = text[:-1]
    try:
        number = float(text)
    except ValueError as exc:
        raise SchemaChanged("invalid numeric count") from exc
    if number < 0:
        raise SchemaChanged("numeric count cannot be negative")
    return int(number * multiplier)


class _PostHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.posts: list[dict] = []
        self.current: dict | None = None
        self.field: str | None = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = set(values.get("class", "").split())
        post_id = values.get("data-post-id") or values.get("data-postid")
        if "articleh" in classes and post_id:
            self.current = {
                "content_id": post_id, "author_id": values.get("data-user-id"),
                "title": "", "text": "",
                "published_at": (values.get("data-publish-time")
                                 or values.get("data-published-at")),
                "last_activity_at": (values.get("data-last-activity")
                                     or values.get("data-last-update")),
                "read_count": 0, "reply_count": 0, "like_count": 0,
            }
            self.posts.append(self.current)
        if self.current is not None:
            if "l3" in classes:
                self.field = "title"
                self.current["url"] = values.get("href")
            elif "publish_time" in classes:
                self.field = "published_at"
            elif ("last_activity" in classes or "last_update" in classes
                  or {"l5", "a5"}.issubset(classes)):
                self.field = "last_activity_at"
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
        self.current[self.field] = _count(value) if self.field.endswith("_count") else value


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
            if values.get("data-parent-comment-id"):
                self.current = None
                return
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
            self.current[self.field] = _count(value) if self.field == "like_count" else value


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
        embedded = self._embedded_article_list(text)
        if embedded is not None:
            rows = embedded.get("re")
            if not isinstance(rows, list):
                raise SchemaChanged("embedded article_list missing re list")
            return [self._post(row) for row in rows if not self._ignored_post(row)]
        article = self._embedded_object(text, "post_article")
        if article is not None:
            row = dict(article)
            user = row.get("post_user")
            if isinstance(user, dict):
                row.setdefault("user_id", user.get("user_id"))
            row["post_content"] = self._plain_text(row.get("post_content"))
            return [] if self._ignored_post(row) else [self._post(row)]
        parser = _PostHTMLParser()
        parser.feed(text)
        if not parser.posts:
            if re.search(r'class=["\'][^"\']*\barticlelist\b', text):
                return []
            raise SchemaChanged("post HTML contains no recognized rows")
        return parser.posts

    def parse_comments(self, body: bytes, content_type: str) -> list[dict]:
        return self.parse_comment_page(body, content_type).records

    def parse_comment_page(self, body: bytes, content_type: str, *,
                           window_start: str | None = None,
                           content_id: str | None = None,
                           page: int = 1) -> CommentPage:
        text = body.decode("utf-8", errors="replace")
        self._ensure_not_blocked(text)
        if "json" not in content_type.lower():
            parser = _CommentHTMLParser()
            parser.feed(text)
            if not parser.comments:
                if re.search(r'class=["\'][^"\']*\bcomment_list\b', text):
                    return CommentPage([], False, None)
                raise SchemaChanged("comment HTML contains no recognized rows")
            records = parser.comments
            filtered = self._filter_window(records, window_start)
            return CommentPage(filtered, False, None,
                               bool(window_start and len(filtered) < len(records)))
        payload = self._json(text)
        rows = payload.get("re")
        if not isinstance(rows, list):
            raise SchemaChanged("comment response missing re list")
        if any(not isinstance(row, dict) for row in rows):
            raise SchemaChanged("comment response rows must be objects")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise SchemaChanged("comment page must be a positive integer")
        records = [self._comment(row, content_id=content_id) for row in rows
                   if not row.get("reply_to_comment_id")]
        filtered = self._filter_window(records, window_start)
        current_contract = any(isinstance(row, dict) and "reply_id" in row for row in rows)
        if current_contract:
            total = payload.get("count", len(rows))
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise SchemaChanged("invalid current comment count")
            exhausted = bool(window_start and len(filtered) < len(records))
            has_more = total > page * 30 and not exhausted
            return CommentPage(filtered, has_more, str(page + 1) if has_more else None,
                               exhausted)
        has_more = payload.get("has_more", False)
        cursor = payload.get("next_cursor")
        if not isinstance(has_more, bool) or (cursor is not None and not isinstance(cursor, (str, int))):
            raise SchemaChanged("invalid comment pagination contract")
        exhausted = bool(window_start and len(filtered) < len(records))
        return CommentPage(filtered, has_more and not exhausted, str(cursor) if cursor is not None else None,
                           exhausted)

    @staticmethod
    def _ensure_not_blocked(text: str) -> None:
        if any(marker in text for marker in _BLOCK_MARKERS):
            raise BlockedResponse("Eastmoney returned an access-control page")

    @staticmethod
    def _filter_window(records: list[dict], window_start: str | None) -> list[dict]:
        if not window_start:
            return records
        try:
            floor = datetime.fromisoformat(str(window_start).replace("Z", "+00:00"))
            output = []
            for row in records:
                published = datetime.fromisoformat(
                    str(row["published_at"]).replace("Z", "+00:00")
                )
                if published.tzinfo is None and floor.tzinfo is not None:
                    published = published.replace(tzinfo=floor.tzinfo)
                elif published.tzinfo is not None and floor.tzinfo is None:
                    floor = floor.replace(tzinfo=published.tzinfo)
                if published >= floor:
                    output.append(row)
            return output
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaChanged("invalid comment timestamp") from exc

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
    def _embedded_article_list(text: str) -> dict | None:
        """Read the public list page's JSON payload without depending on its table markup."""
        return EastmoneyParser._embedded_object(text, "article_list")

    @staticmethod
    def _embedded_object(text: str, variable: str) -> dict | None:
        marker = re.search(rf"\bvar\s+{re.escape(variable)}\s*=\s*", text)
        if marker is None:
            return None
        try:
            value, _ = json.JSONDecoder().raw_decode(text[marker.end():])
        except json.JSONDecodeError as exc:
            raise SchemaChanged(f"invalid embedded {variable}") from exc
        if not isinstance(value, dict):
            raise SchemaChanged(f"embedded {variable} must be an object")
        return value

    @staticmethod
    def _plain_text(value: object) -> str:
        text = re.sub(r"<[^>]+>", " ", str(value or ""))
        return " ".join(unescape(text).split())

    @staticmethod
    def _ignored_post(row: dict) -> bool:
        return bool(row.get("is_ad") or row.get("is_notice") or row.get("post_type") in {1, 2})

    @staticmethod
    def _post(row: dict) -> dict:
        required = ("post_id", "post_publish_time")
        if any(not row.get(key) for key in required):
            raise SchemaChanged("post row missing identity or time")
        forum_id = str(row.get("stockbar_code") or "").strip()
        post_id = str(row["post_id"])
        url = row.get("post_url")
        if not url and forum_id:
            url = f"https://guba.eastmoney.com/news,{forum_id},{post_id}.html"
        return {
            "content_id": post_id, "comment_id": "",
            "title": str(row.get("post_title") or ""), "text": str(row.get("post_content") or ""),
            "published_at": row["post_publish_time"], "author_id": row.get("user_id"),
            "last_activity_at": row.get("post_last_time") or row["post_publish_time"],
            "url": url,
            "read_count": _count(row.get("post_click_count")),
            "reply_count": _count(row.get("post_comment_count")),
            "like_count": _count(row.get("post_like_count")),
        }

    @staticmethod
    def _comment(row: dict, *, content_id: str | None = None) -> dict:
        comment_id = row.get("comment_id") or row.get("reply_id")
        post_id = row.get("post_id") or content_id
        published_at = row.get("comment_publish_time") or row.get("reply_publish_time")
        if not comment_id or not post_id or not published_at:
            raise SchemaChanged("comment row missing identity, parent, or time")
        reply_user = row.get("reply_user")
        author_id = (reply_user.get("user_id") if isinstance(reply_user, dict)
                     else row.get("user_id"))
        return {
            "content_id": str(post_id), "comment_id": str(comment_id),
            "parent_content_id": str(post_id), "title": "",
            "text": str(row.get("comment_content") or row.get("reply_text") or ""),
            "published_at": published_at, "author_id": author_id,
            "url": row.get("post_url"),
            "read_count": 0, "reply_count": 0,
            "like_count": _count(row.get("comment_like_count")
                                  if "comment_like_count" in row
                                  else row.get("reply_like_count")),
        }
