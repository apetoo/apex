"""Evidence normalization and source-quality policy for stock research."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TIER_1_HOSTS = (
    "cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn", "csrc.gov.cn",
    "gov.cn",
)
_TIER_2_HOSTS = (
    "eastmoney.com", "10jqka.com.cn", "sina.com.cn",
    "qq.com", "163.com", "ifeng.com", "stockstar.com", "stcn.com",
    "cs.com.cn", "cnstock.com",
)
_TIER_3_HOSTS = ("guba.eastmoney.com", "caifuhao.eastmoney.com", "xueqiu.com")
_TIER_3_MARKERS = ("股吧", "财富号", "自媒体", "博客")

_CATEGORY_TERMS = {
    "earnings": ("业绩", "营收", "利润", "财报", "季报", "年报", "预告", "销售"),
    "shareholders": ("股东", "减持", "增持", "解禁", "大宗交易", "回购"),
    "regulatory": ("立案", "处罚", "诉讼", "问询", "监管", "警示", "违规", "涉税"),
    "money_flow": ("北向", "龙虎榜", "主力", "机构", "资金", "席位"),
    "corporate_actions": ("定增", "配股", "回购", "重组", "并购", "收购"),
    "research": ("研报", "评级", "目标价", "一致预期"),
    "industry": ("政策", "景气", "需求", "补贴", "供给", "价格"),
}


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def source_tier(url: str, site_name: str = "") -> int:
    """Classify trust from URL hostname; display names are fallback only."""
    host = _hostname(url)
    if any(host == h or host.endswith(f".{h}") for h in _TIER_3_HOSTS):
        return 3
    if any(host == h or host.endswith(f".{h}") for h in _TIER_1_HOSTS):
        return 1
    if any(host == h or host.endswith(f".{h}") for h in _TIER_2_HOSTS):
        return 2
    if any(marker in (site_name or "") for marker in _TIER_3_MARKERS):
        return 3
    return 3


def _canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = [
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"spm", "from", "scm"}
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def _entity_matches(text: str, ts_code: str, name: str) -> bool:
    compact = _compact(text)
    code = ts_code.split(".")[0].lower()
    qualified = ts_code.lower()
    return bool((name and _compact(name) in compact) or code in compact or qualified in compact)


def _category_matches(text: str, category: str) -> bool:
    terms = _CATEGORY_TERMS.get(category)
    return True if not terms else any(term in text for term in terms)


def _freshness(value: str, today: date) -> tuple[str, str]:
    if not value:
        return "unknown", ""
    try:
        parsed = datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return "invalid", value
    if parsed > today:
        return "future", parsed.isoformat()
    return "current", parsed.isoformat()


def normalize_search_results(
    rows: list[dict[str, Any]], *, ts_code: str, name: str, category: str,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter and annotate web results before they enter model context."""
    today = today or date.today()
    accepted: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    filtered = duplicates = 0

    for raw in sorted(rows, key=lambda row: source_tier(str(row.get("url") or ""), str(row.get("site") or ""))):
        title = str(raw.get("title") or "").strip()
        snippet = str(raw.get("snippet") or "").strip().replace("\n", " ")
        combined = f"{title} {snippet}"
        entity_matched = _entity_matches(combined, ts_code, name)
        freshness_status, published_at = _freshness(str(raw.get("date") or ""), today)
        if (category != "industry" and not entity_matched) or not _category_matches(combined, category):
            filtered += 1
            continue
        if freshness_status in {"future", "invalid"}:
            filtered += 1
            continue

        url = _canonical_url(str(raw.get("url") or ""))
        title_fingerprint = re.sub(r"[^\w\u4e00-\u9fff]", "", _compact(title))
        if url in seen_urls or (title_fingerprint and title_fingerprint in seen_titles):
            duplicates += 1
            continue
        seen_urls.add(url)
        seen_titles.add(title_fingerprint)
        tier = source_tier(url, str(raw.get("site") or ""))
        accepted.append({
            **raw,
            "url": url,
            "date": published_at,
            "source_tier": tier,
            "entity_matched": entity_matched,
            "freshness_status": freshness_status,
        })

    accepted.sort(key=lambda row: (
        row["source_tier"],
        -(int((row.get("date") or "0000-00-00").replace("-", ""))),
    ))
    tier_counts = {str(tier): sum(1 for row in accepted if row["source_tier"] == tier) for tier in (1, 2, 3)}
    return accepted, {
        "raw_count": len(rows),
        "accepted_count": len(accepted),
        "filtered_count": filtered,
        "duplicate_count": duplicates,
        "tier_counts": tier_counts,
    }


def make_evidence_item(
    *, fact: str, inference: str, evidence_type: str, tool_name: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    identity = "|".join((fact, tool_name, str(source.get("url") or ""), str(source.get("date") or "")))
    return {
        "id": f"ev_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]}",
        "fact": fact,
        "inference": inference,
        "evidence_type": evidence_type,
        "tool_name": tool_name,
        "source_name": source.get("site") or source.get("title") or tool_name,
        "source_url": source.get("url") or None,
        "published_at": source.get("date") or None,
        "source_tier": int(source.get("source_tier") or 1),
        "entity_matched": bool(source.get("entity_matched", True)),
        "freshness_status": source.get("freshness_status") or "current",
    }
