"""Manage ~/.stock-watchlist/watchlist.json: positions, candidates, archive."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import config

_TZ_CN = timezone(timedelta(hours=8))


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
            if section == "active_positions" and not item.get("avg_cost"):
                ep = item.get("entry_price")
                if ep is not None:
                    item["avg_cost"] = float(ep)
                    changed = True
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
                 stop_loss: Optional[float], target: Optional[float],
                 trigger_price: Optional[float] = None,
                 trigger_direction: str = "below",
                 expires_days: int = 10,
                 position_size_shares: Optional[int] = None,
                 risk_amount: Optional[float] = None,
                 calibrated_confidence: Optional[float] = None,
                 strategy: Optional[str] = None,
                 regime_at_open: Optional[str] = None) -> None:
    """Add new position. Raises DuplicatePositionError if ts_code already in active_positions.

    可选字段缺省时不写入对应 key（保持老记录兼容）：
      position_size_shares / risk_amount / calibrated_confidence / strategy
    strategy 是策略归属（screener 4 个策略名之一，或 'manual'/'analyze' 等），用于
    calibration.compute() 切 by_strategy 桶 + 长期评估各策略胜率。"""
    from datetime import timedelta
    data = _load()
    for existing in data["active_positions"]:
        if existing.get("ts_code") == ts_code:
            raise DuplicatePositionError(ts_code, existing)

    expires = (date.today() + timedelta(days=expires_days)).isoformat()
    record: dict = {
        "ts_code": ts_code,
        "name": name,
        "entry_price": entry_price,
        "avg_cost": float(entry_price),
        "entry_date": date.today().isoformat(),
        "trigger_price": trigger_price,
        "trigger_direction": trigger_direction,
        "expires_at": expires,
        "status": "active",
    }
    if stop_loss is not None:
        record["stop_loss"] = float(stop_loss)
    if target is not None:
        record["target"] = float(target)
    if position_size_shares is not None and position_size_shares > 0:
        record["position_size_shares"] = int(position_size_shares)
    if risk_amount is not None:
        record["risk_amount"] = float(risk_amount)
    if calibrated_confidence is not None:
        record["calibrated_confidence"] = float(calibrated_confidence)
    if strategy:
        record["strategy"] = str(strategy)
    # regime_at_open：调用方未传则从今日 regime 缓存取。无缓存就空（不主动 collect 避免延迟）
    if regime_at_open is None:
        try:
            from apex import regime as _regime_mod
            today_iso = date.today().isoformat()
            r = _regime_mod.load(today_iso)
            if r and r.get("label"):
                regime_at_open = r["label"]
        except Exception:
            regime_at_open = None
    if regime_at_open:
        record["regime_at_open"] = str(regime_at_open)
    data["active_positions"].append(record)
    _save(data)


def replace_position(ts_code: str, name: str, entry_price: float,
                     stop_loss: float, target: float,
                     trigger_price: Optional[float] = None,
                     trigger_direction: str = "below",
                     expires_days: int = 10,
                     position_size_shares: Optional[int] = None,
                     risk_amount: Optional[float] = None,
                     calibrated_confidence: Optional[float] = None,
                     strategy: Optional[str] = None,
                     regime_at_open: Optional[str] = None) -> None:
    """Archive existing position with reason='replaced', then add new one."""
    archive_entry(ts_code, "active_positions", reason="replaced")
    add_position(ts_code, name, entry_price, stop_loss, target,
                 trigger_price, trigger_direction, expires_days,
                 position_size_shares=position_size_shares,
                 risk_amount=risk_amount,
                 calibrated_confidence=calibrated_confidence,
                 strategy=strategy,
                 regime_at_open=regime_at_open)


def promote_candidate(ts_code: str, entry_price: float,
                      stop_loss: float, target: float,
                      expires_days: int = 10,
                      position_size_shares: Optional[int] = None,
                      risk_amount: Optional[float] = None,
                      calibrated_confidence: Optional[float] = None,
                      strategy: Optional[str] = None,
                      regime_at_open: Optional[str] = None) -> None:
    """Promote a candidate to active position. Archives the candidate with
    reason='promoted', then adds the active position using actual fill values.

    strategy 缺省时从候选 record 自动继承（候选时如果带了 strategy 字段）。
    """
    wl = _load()
    existing = next((p for p in wl["active_positions"] if p.get("ts_code") == ts_code), None)
    if existing is not None:
        raise DuplicatePositionError(ts_code, existing)
    cand = next((c for c in wl["candidates"] if c.get("ts_code") == ts_code), None)
    if cand is None:
        raise ValueError(f"候选 {ts_code} 不存在")
    name = cand.get("name", "")
    inherited_strategy = strategy or cand.get("strategy")
    archive_entry(ts_code, "candidates", reason="promoted")
    add_position(ts_code, name, entry_price, stop_loss, target,
                 trigger_price=None, trigger_direction="below",
                 expires_days=expires_days,
                 position_size_shares=position_size_shares,
                 risk_amount=risk_amount,
                 calibrated_confidence=calibrated_confidence,
                 strategy=inherited_strategy,
                 regime_at_open=regime_at_open)


def _today_regime() -> Optional[str]:
    """今日 regime label(无缓存返回 None, 不主动 collect 避免延迟)。"""
    try:
        from apex import regime as _regime_mod
        today_iso = date.today().isoformat()
        r = _regime_mod.load(today_iso)
        if r and r.get("label"):
            return r["label"]
    except Exception:
        pass
    return None


def _journal_ref_for(ts_code: str) -> Optional[dict]:
    """下单时该股最近一条 journal entry(无则 None)。供 AI 诊断关联开仓上下文。"""
    try:
        from apex import journal as _journal
        entries = _journal.load_entries(ts_code=ts_code)
        if not entries:
            return None
        latest = sorted(entries, key=lambda e: e.get("analyzed_at") or e.get("date", ""))[-1]
        return {
            "verdict": latest.get("verdict"),
            "confidence": latest.get("confidence"),
            "analyzed_at": latest.get("analyzed_at") or latest.get("date"),
        }
    except Exception:
        return None


def buy(ts_code: str, fill_price: float, shares: int,
        stop_loss: Optional[float] = None, target: Optional[float] = None,
        note: str = "", strategy: str = "manual",
        regime: Optional[str] = None) -> dict:
    """买入:持仓存在则加仓(重算 avg_cost),不存在则开仓。同时追加一条 buy trade 留痕。

    返回 {"position": <更新后 record>, "trade": <写入的 trade>}。

    Raises:
      ValueError: fill_price<=0 或 shares<=0
    """
    from apex import data as _data
    from apex import trades as _trades

    if fill_price is None or float(fill_price) <= 0:
        raise ValueError(f"fill_price 必须 > 0, got {fill_price}")
    if shares is None or int(shares) <= 0:
        raise ValueError(f"shares 必须 > 0, got {shares}")

    fill_price = float(fill_price)
    shares = int(shares)
    wl = _load()
    existing = next((p for p in wl["active_positions"] if p.get("ts_code") == ts_code), None)

    regime_label = regime if regime is not None else _today_regime()
    journal_ref = _journal_ref_for(ts_code)

    if existing is not None:
        # 加仓
        old_shares = int(existing.get("position_size_shares") or 0)
        old_avg = float(existing.get("avg_cost") or existing.get("entry_price") or 0)
        new_shares = old_shares + shares
        if new_shares > 0:
            new_avg = (old_avg * old_shares + fill_price * shares) / new_shares
        else:
            new_avg = fill_price
        existing["position_size_shares"] = new_shares
        existing["avg_cost"] = round(new_avg, 4)
        if stop_loss is not None:
            existing["stop_loss"] = float(stop_loss)
        if target is not None:
            existing["target"] = float(target)
        position = existing
    else:
        # 开仓: 反查名称
        name = ""
        try:
            info_raw = _data.get_stock_info(ts_code=ts_code)
            info_list = json.loads(info_raw) if isinstance(info_raw, str) else info_raw
            if isinstance(info_list, list) and info_list:
                name = info_list[0].get("name", "") or ""
        except Exception:
            pass
        record = {
            "ts_code": ts_code,
            "name": name,
            "entry_price": fill_price,
            "avg_cost": fill_price,
            "entry_date": date.today().isoformat(),
            "trigger_price": None,
            "trigger_direction": "below",
            "expires_at": (date.today() + timedelta(days=10)).isoformat(),
            "status": "active",
        }
        if stop_loss is not None:
            record["stop_loss"] = float(stop_loss)
        if target is not None:
            record["target"] = float(target)
        record["position_size_shares"] = shares
        if strategy:
            record["strategy"] = str(strategy)
        wl["active_positions"].append(record)
        position = record

    _save(wl)
    trade = _trades.append_trade(
        ts_code=ts_code, name=position.get("name", ""),
        side="buy", fill_price=fill_price, shares=shares,
        avg_cost_after=position.get("avg_cost"), shares_after=position.get("position_size_shares"),
        strategy=position.get("strategy") or strategy, regime=regime_label,
        journal_ref=journal_ref, realized_pnl=None, note=note,
    )
    return {"position": position, "trade": trade}


def sell(ts_code: str, fill_price: float, shares: int,
         exit_reason: str = "manual", note: str = "",
         postmortem: bool = True) -> dict:
    """卖出:减仓(改股数+实现 pnl,不复盘)或卖光(走 close_position + postmortem + calibration)。
    同时追加一条 sell trade 留痕。

    返回:
      减仓: {"position": <更新后 pos>, "trade": <sell trade>}
      卖光: {"trade": <sell trade>, "closed_record": <closed record>, "diagnosis": <diagnosis or None>}

    Raises:
      PositionNotFoundError: ts_code 不在 active_positions
      ValueError: fill_price<=0 / shares<=0 / shares > 持有
    """
    from apex import trades as _trades
    from apex.schemas import EXIT_REASON_ENUM

    if fill_price is None or float(fill_price) <= 0:
        raise ValueError(f"fill_price 必须 > 0, got {fill_price}")
    if shares is None or int(shares) <= 0:
        raise ValueError(f"shares 必须 > 0, got {shares}")
    if exit_reason not in EXIT_REASON_ENUM:
        exit_reason = "other"

    fill_price = float(fill_price)
    shares = int(shares)
    wl = _load()
    pos = next((p for p in wl["active_positions"] if p.get("ts_code") == ts_code), None)
    if pos is None:
        raise PositionNotFoundError(f"持仓 {ts_code} 不存在于 active_positions")

    holding = int(pos.get("position_size_shares") or 0)
    if shares > holding:
        raise ValueError(f"卖出股数 {shares} 超过持有 {holding}")

    avg_cost_before = float(pos.get("avg_cost") or pos.get("entry_price") or 0)
    regime_label = _today_regime()
    journal_ref = _journal_ref_for(ts_code)
    name = pos.get("name", "")
    strategy = pos.get("strategy")

    if shares >= holding:
        # 卖光 → 留痕 + 走 close_position(actual_fill_price=avg_cost_before 使 pnl 基准正确)
        realized = round((fill_price - avg_cost_before) * holding, 2) if avg_cost_before > 0 else None
        realized_pct = round((fill_price / avg_cost_before - 1), 4) if avg_cost_before > 0 else None
        trade = _trades.append_trade(
            ts_code=ts_code, name=name, side="sell",
            fill_price=fill_price, shares=holding,
            avg_cost_after=None, shares_after=0,
            strategy=strategy, regime=regime_label, journal_ref=journal_ref,
            realized_pnl=realized, realized_pnl_pct=realized_pct, note=note,
        )
        closed_record = close_position(
            ts_code=ts_code, exit_price=fill_price, exit_reason=exit_reason,
            actual_fill_price=avg_cost_before,
        )
        diagnosis = None
        if postmortem:
            try:
                from apex import postmortem as _pm
                diagnosis = _pm.run_and_patch(closed_record)
            except Exception:
                diagnosis = None
            try:
                from apex import calibration as _cal
                _cal.compute()
            except Exception:
                pass
        return {"trade": trade, "closed_record": closed_record, "diagnosis": diagnosis}

    # 减仓 → 留痕 + 改状态, 不复盘
    realized = round((fill_price - avg_cost_before) * shares, 2) if avg_cost_before > 0 else None
    realized_pct = round((fill_price / avg_cost_before - 1), 4) if avg_cost_before > 0 else None
    new_shares = holding - shares
    pos["position_size_shares"] = new_shares  # avg_cost 不变
    _save(wl)
    trade = _trades.append_trade(
        ts_code=ts_code, name=name, side="sell",
        fill_price=fill_price, shares=shares,
        avg_cost_after=avg_cost_before, shares_after=new_shares,
        strategy=strategy, regime=regime_label, journal_ref=journal_ref,
        realized_pnl=realized, realized_pnl_pct=realized_pct, note=note,
    )
    return {"position": pos, "trade": trade}


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


def _closed_positions_path() -> Path:
    cfg = config.get()
    journal_dir = Path(cfg["paths"]["journal_dir"]).expanduser()
    return journal_dir / "closed_positions.jsonl"


def _fetch_holding_period_extremes(ts_code: str, entry_date: str, exit_date: str) -> dict:
    """从 K 线拉持仓期间的 high/low/days_held。失败返回 {} 不阻塞平仓主流程。"""
    try:
        from apex import data as _data
        start = entry_date.replace("-", "")
        end = exit_date.replace("-", "")
        raw = _data.get_daily_price(ts_code, start_date=start, end_date=end, adj="qfq")
        rows = json.loads(raw)
        if not rows or not isinstance(rows, list):
            return {}
        highs = [r["high"] for r in rows if r.get("high") is not None]
        lows = [r["low"] for r in rows if r.get("low") is not None]
        return {
            "high_during_hold": round(max(highs), 3) if highs else None,
            "low_during_hold": round(min(lows), 3) if lows else None,
            "trading_days": len(rows),
        }
    except Exception:
        return {}


def _latest_journal_for(ts_code: str, before: Optional[str] = None) -> Optional[dict]:
    """取该股最近一条 journal 记录（可选限定 before 日期之前），用于反向关联开仓时 AI 上下文。"""
    try:
        from apex import journal as _journal
        entries = _journal.load_entries(ts_code=ts_code)
        if before:
            entries = [e for e in entries if (e.get("date") or "") <= before]
        if not entries:
            return None
        return sorted(entries, key=lambda e: e.get("analyzed_at") or e.get("date", ""))[-1]
    except Exception:
        return None


class PositionNotFoundError(Exception):
    pass


def close_position(ts_code: str,
                   exit_price: float,
                   exit_reason: str = "manual",
                   exit_date: Optional[str] = None,
                   user_notes: str = "",
                   actual_fill_price: Optional[float] = None) -> dict:
    """平仓一笔持仓 → 写一条完整记录到 closed_positions.jsonl，并从 active_positions 移除。

    返回写入的 closed record（不含 diagnosis 段，那是 1.4 post-mortem 的活）。

    actual_fill_price: 开仓时的实际成交价（A股可能与 entry_price 略有滑点）；缺省回落到 entry_price。

    Raises:
      PositionNotFoundError: ts_code 不在 active_positions 中
      ValueError: exit_price 非正
    """
    from apex.schemas import EXIT_REASON_ENUM

    if exit_price is None or float(exit_price) <= 0:
        raise ValueError(f"exit_price 必须 > 0，got {exit_price}")
    if exit_reason not in EXIT_REASON_ENUM:
        exit_reason = "other"

    today_str = date.today().isoformat()
    if not exit_date:
        exit_date = today_str

    wl = _load()
    pos = next((p for p in wl["active_positions"] if p.get("ts_code") == ts_code), None)
    if pos is None:
        raise PositionNotFoundError(f"持仓 {ts_code} 不存在于 active_positions")

    entry_date = pos.get("entry_date") or today_str
    entry_price = float(pos.get("entry_price") or 0)
    avg_cost = float(pos.get("avg_cost") or entry_price)
    fill_price = float(actual_fill_price) if actual_fill_price is not None else avg_cost
    shares = int(pos.get("position_size_shares") or 0)

    extremes = _fetch_holding_period_extremes(ts_code, entry_date, exit_date)

    days_held = 0
    try:
        d_in = date.fromisoformat(entry_date)
        d_out = date.fromisoformat(exit_date)
        days_held = max(0, (d_out - d_in).days)
    except Exception:
        pass

    realized_pnl_pct = None
    realized_pnl_amount = None
    if fill_price > 0:
        realized_pnl_pct = round((float(exit_price) - fill_price) / fill_price, 4)
        if shares > 0:
            realized_pnl_amount = round((float(exit_price) - fill_price) * shares, 2)

    journal_at_open = _latest_journal_for(ts_code, before=entry_date)

    record = {
        "ts_code": ts_code,
        "name": pos.get("name", ""),
        "open": {
            "entry_date": entry_date,
            "entry_price": entry_price or None,
            "avg_cost": avg_cost or None,
            "actual_fill_price": fill_price or None,
            "position_size_shares": shares or None,
            "stop_loss": pos.get("stop_loss"),
            "target": pos.get("target"),
            "risk_amount": pos.get("risk_amount"),
            "calibrated_confidence": pos.get("calibrated_confidence"),
            "strategy": pos.get("strategy"),
            "regime_at_open": pos.get("regime_at_open"),
            "ai_verdict": (journal_at_open or {}).get("verdict"),
            "ai_confidence": (journal_at_open or {}).get("confidence"),
            "ai_features": (journal_at_open or {}).get("features"),
            "ai_analysis_text": (journal_at_open or {}).get("analysis_text"),
            "screener_signals": None,
            "screener_rule_score": None,
        },
        "close": {
            "exit_date": exit_date,
            "actual_exit_price": float(exit_price),
            "exit_reason": exit_reason,
            "days_held": days_held,
            "trading_days_held": extremes.get("trading_days"),
            "high_during_hold": extremes.get("high_during_hold"),
            "low_during_hold": extremes.get("low_during_hold"),
            "realized_pnl_pct": realized_pnl_pct,
            "realized_pnl_amount": realized_pnl_amount,
            "user_notes": user_notes or "",
            "closed_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
        },
        "diagnosis": None,
    }

    out_path = _closed_positions_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    wl["active_positions"] = [p for p in wl["active_positions"] if p.get("ts_code") != ts_code]
    pos_breadcrumb = dict(pos)
    pos_breadcrumb["status"] = f"closed_{exit_reason}"
    pos_breadcrumb["archived_date"] = today_str
    pos_breadcrumb["archived_from"] = "active_positions"
    pos_breadcrumb["closed_record_ref"] = record["close"]["closed_at"]
    wl["archived"].append(pos_breadcrumb)
    _save(wl)

    return record


def load_closed_positions(limit: Optional[int] = None,
                          since_days: Optional[int] = None) -> list[dict]:
    """读 closed_positions.jsonl，按 closed_at 倒序。limit / since_days 二选一可选。"""
    path = _closed_positions_path()
    if not path.exists():
        return []
    out: list[dict] = []
    cutoff: Optional[str] = None
    if since_days:
        cutoff = (datetime.now(_TZ_CN) - timedelta(days=since_days)).isoformat()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff:
                closed_at = (rec.get("close") or {}).get("closed_at") or ""
                if closed_at < cutoff:
                    continue
            out.append(rec)
    out.sort(key=lambda r: (r.get("close") or {}).get("closed_at") or "", reverse=True)
    if limit:
        out = out[:limit]
    return out


def add_candidate(ts_code: str, name: str, trigger_price: float,
                  trigger_direction: str = "above",
                  note: str = "",
                  expires_days: int = 7,
                  stop_advice: Optional[float] = None,
                  target_advice: Optional[float] = None,
                  strategy: Optional[str] = None) -> None:
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
    if strategy:
        entry["strategy"] = str(strategy)
    data["candidates"].append(entry)
    _save(data)
