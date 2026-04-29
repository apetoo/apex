"""Manage ~/.stock-watchlist/watchlist.json: positions, candidates, archive."""
import json
from datetime import date
from pathlib import Path
from typing import Optional

from apex import config


class DuplicatePositionError(Exception):
    """Raised when adding a position whose ts_code already exists in active_positions."""
    def __init__(self, ts_code: str, existing: dict):
        self.ts_code = ts_code
        self.existing = existing
        super().__init__(f"持仓 {ts_code} 已存在")


def _watchlist_path() -> Path:
    return Path(config.get()["paths"]["watchlist_file"])


def _load() -> dict:
    path = _watchlist_path()
    if not path.exists():
        return {"active_positions": [], "candidates": [], "archived": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    path = _watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_triggered(entry: dict, current_price: float) -> bool:
    direction = entry.get("trigger_direction", "below")
    trigger = entry.get("trigger_price")
    if trigger is None:
        return False
    if direction == "below":
        return current_price <= trigger
    if direction == "above":
        return current_price >= trigger
    return False


def expire_stale(data: dict) -> list[str]:
    """Move expired entries to archived. Returns list of expired ts_codes."""
    today = date.today().isoformat()
    expired = []
    for section in ("active_positions", "candidates"):
        surviving = []
        for item in data[section]:
            if item.get("expires_at") and item["expires_at"] < today:
                item["status"] = "expired"
                item["archived_date"] = today
                data["archived"].append(item)
                expired.append(item["ts_code"])
            else:
                surviving.append(item)
        data[section] = surviving
    return expired


def get_triggered(current_prices: dict[str, float]) -> tuple[list[dict], list[dict]]:
    """
    Check watchlist against current prices.
    Returns (triggered_positions, triggered_candidates).
    """
    data = _load()
    expire_stale(data)
    _save(data)

    triggered_pos = []
    triggered_cand = []

    for item in data["active_positions"]:
        price = current_prices.get(item["ts_code"])
        if price and is_triggered(item, price):
            triggered_pos.append({**item, "current_price": price})

    for item in data["candidates"]:
        price = current_prices.get(item["ts_code"])
        if price and is_triggered(item, price):
            triggered_cand.append({**item, "current_price": price})

    return triggered_pos, triggered_cand


def load() -> dict:
    return _load()


def migrate_and_backfill() -> dict:
    """Normalize ts_codes (add exchange suffix) and look up missing names. Persists if changed.
    Also merges legacy unsuffixed journal files and dedups active_positions."""
    from apex import data as _data
    from apex import journal as _journal
    changed = False
    wl = _load()
    stats = {"normalized": 0, "named": 0, "journal_merged": 0, "deduped": 0}
    for section in ("active_positions", "candidates", "archived"):
        for item in wl.get(section, []):
            old = item.get("ts_code", "")
            new = _data.normalize_ts_code(old)
            if new and new != old:
                item["ts_code"] = new
                stats["normalized"] += 1
                changed = True
            if section in ("active_positions", "candidates") and not item.get("name") and item.get("ts_code"):
                try:
                    info_raw = _data.get_stock_info(ts_code=item["ts_code"])
                    info_list = json.loads(info_raw)
                    if isinstance(info_list, list) and info_list:
                        nm = info_list[0].get("name", "")
                        if nm:
                            item["name"] = nm
                            stats["named"] += 1
                            changed = True
                except Exception:
                    pass
    if changed:
        _save(wl)

    try:
        merge_stats = _journal.merge_legacy_files()
        stats["journal_merged"] = merge_stats.get("merged", 0)
    except Exception:
        pass

    try:
        stats["deduped"] = dedup_active_positions()
    except Exception:
        pass

    return stats


def add_position(ts_code: str, name: str, entry_price: float,
                 stop_loss: float, target: float,
                 trigger_price: Optional[float] = None,
                 trigger_direction: str = "below",
                 expires_days: int = 10) -> None:
    """Add new position. Raises DuplicatePositionError if ts_code already in active_positions."""
    from datetime import timedelta
    data = _load()
    for existing in data["active_positions"]:
        if existing.get("ts_code") == ts_code:
            raise DuplicatePositionError(ts_code, existing)

    expires = (date.today() + timedelta(days=expires_days)).isoformat()
    data["active_positions"].append({
        "ts_code": ts_code,
        "name": name,
        "entry_price": entry_price,
        "entry_date": date.today().isoformat(),
        "stop_loss": stop_loss,
        "target": target,
        "trigger_price": trigger_price,  # None = 不触发；is_triggered 已处理 None
        "trigger_direction": trigger_direction,
        "expires_at": expires,
        "status": "active",
    })
    _save(data)


def replace_position(ts_code: str, name: str, entry_price: float,
                     stop_loss: float, target: float,
                     trigger_price: Optional[float] = None,
                     trigger_direction: str = "below",
                     expires_days: int = 10) -> None:
    """Archive existing position with reason='replaced', then add new one."""
    archive_entry(ts_code, "active_positions", reason="replaced")
    add_position(ts_code, name, entry_price, stop_loss, target,
                 trigger_price, trigger_direction, expires_days)


def promote_candidate(ts_code: str, entry_price: float,
                      stop_loss: float, target: float,
                      expires_days: int = 10) -> None:
    """Promote a candidate to active position. Archives the candidate with
    reason='promoted', then adds the active position using actual fill values.
    Raises DuplicatePositionError if ts_code already in active_positions
    (candidate is left intact in that case so the caller can resolve the conflict).
    Raises ValueError if candidate doesn't exist."""
    wl = _load()
    existing = next((p for p in wl["active_positions"] if p.get("ts_code") == ts_code), None)
    if existing is not None:
        raise DuplicatePositionError(ts_code, existing)
    cand = next((c for c in wl["candidates"] if c.get("ts_code") == ts_code), None)
    if cand is None:
        raise ValueError(f"候选 {ts_code} 不存在")
    name = cand.get("name", "")
    archive_entry(ts_code, "candidates", reason="promoted")
    add_position(ts_code, name, entry_price, stop_loss, target,
                 trigger_price=None, trigger_direction="below",
                 expires_days=expires_days)


def dedup_active_positions() -> int:
    """
    Collapse duplicate ts_codes in active_positions: keep the one with the
    latest entry_date (tiebreak: keep last in list), archive the rest with
    reason='dedup'. Returns count archived.
    """
    wl = _load()
    today = date.today().isoformat()
    seen: dict[str, dict] = {}
    no_code: list[dict] = []
    archived_count = 0
    for item in wl["active_positions"]:
        code = item.get("ts_code")
        if not code:
            no_code.append(item)
            continue
        if code not in seen:
            seen[code] = item
            continue
        prev = seen[code]
        if (item.get("entry_date") or "") >= (prev.get("entry_date") or ""):
            loser, winner = prev, item
        else:
            loser, winner = item, prev
        wl["archived"].append({
            **loser, "status": "archived_dedup",
            "archived_date": today, "archived_from": "active_positions",
        })
        seen[code] = winner
        archived_count += 1
    if archived_count:
        wl["active_positions"] = no_code + list(seen.values())
        _save(wl)
    return archived_count


def archive_entry(ts_code: str, section: str, reason: str = "manual") -> bool:
    """Move an entry from active_positions or candidates into archived. Returns True if moved."""
    if section not in ("active_positions", "candidates"):
        return False
    wl = _load()
    today = date.today().isoformat()
    for i, item in enumerate(wl[section]):
        if item.get("ts_code") == ts_code:
            item["status"] = f"archived_{reason}"
            item["archived_date"] = today
            item["archived_from"] = section
            wl["archived"].append(item)
            wl[section].pop(i)
            _save(wl)
            return True
    return False


def add_candidate(ts_code: str, name: str, trigger_price: float,
                  trigger_direction: str = "above",
                  note: str = "",
                  expires_days: int = 7,
                  stop_advice: Optional[float] = None,
                  target_advice: Optional[float] = None) -> None:
    from datetime import timedelta
    data = _load()
    expires = (date.today() + timedelta(days=expires_days)).isoformat()
    entry: dict = {
        "ts_code": ts_code,
        "name": name,
        "trigger_price": trigger_price,
        "trigger_direction": trigger_direction,
        "expires_at": expires,
        "note": note,
    }
    if stop_advice is not None:
        entry["stop_advice"] = stop_advice
    if target_advice is not None:
        entry["target_advice"] = target_advice
    data["candidates"].append(entry)
    _save(data)
