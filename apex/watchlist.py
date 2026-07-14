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


def _notify(event_type: str, *, ts_code: str, name: str = "",
             before: Optional[dict] = None, after: Optional[dict] = None,
             trade: Optional[dict] = None, close: Optional[dict] = None,
             exit_reason: Optional[str] = None) -> None:
    """持仓变更推送钩子（旁路）。推送失败/未启用都不阻断主流程。

    before/after 为内部 active_positions record（push 模块内部做对外字段映射）。
    """
    try:
        from apex import push as _push
        _push.notify(event_type, ts_code=ts_code, name=name,
                     before=before, after=after, trade=trade, close=close,
                     exit_reason=exit_reason)
    except Exception:  # noqa: BLE001
        pass


def is_triggered(entry: dict, current_price: float) -> bool:
    # 带状触发优先：有 trigger_low/trigger_high 时，价格进入此带才触发（避免接飞刀/追高）。
    low = entry.get("trigger_low")
    high = entry.get("trigger_high")
    if low is not None and high is not None:
        return float(low) <= current_price <= float(high)
    # 退回单向阈值（老候选 / 持仓层 trigger_price=None 不触发）
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
            if section in ("active_positions", "candidates") and item.get("ts_code"):
                # name 缺失, 或被误存成 ts_code/裸代码(AddCandidateDialog 历史bug) → 重查
                nm = item.get("name", "")
                bare = item["ts_code"].split(".")[0]
                if not nm or nm == item["ts_code"] or nm == bare:
                    try:
                        info_raw = _data.get_stock_info(ts_code=item["ts_code"])
                        info_list = json.loads(info_raw)
                        if isinstance(info_list, list) and info_list:
                            real = info_list[0].get("name", "")
                            if real and real != nm:
                                item["name"] = real
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

    try:
        stats["candidates_deduped"] = dedup_candidates()
    except Exception:
        pass

    # PR4: closed_positions 回填（regime 从缓存 / sector 走 tushare）+ trades 重建
    try:
        bf = _backfill_closed_open_fields()
        stats["closed_regime_filled"] = bf.get("regime_filled", 0)
        stats["closed_sector_filled"] = bf.get("sector_filled", 0)
        stats["closed_price_advice_filled"] = bf.get("price_advice_filled", 0)
    except Exception:
        pass
    try:
        stats["closed_reconstructed"] = _reconstruct_closed_from_trades()
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
                 regime_at_open: Optional[str] = None,
                 size_method: Optional[str] = None,
                 setup: Optional[str] = None,
                 rule_checklist: Optional[dict] = None,
                 emit_notify: bool = True) -> dict:
    """Add new position. Raises DuplicatePositionError if ts_code already in active_positions.

    可选字段缺省时不写入对应 key（保持老记录兼容）：
      position_size_shares / risk_amount / calibrated_confidence / strategy
    strategy 是策略归属（screener 4 个策略名之一，或 'manual'/'analyze' 等），用于
    calibration.compute() 切 by_strategy 桶 + 长期评估各策略胜率。
    size_method 标注手数来源（'atr_estimated' / 'missing'），仅 promote 兜底推算时传；
      不传 = 用户明确手填或老记录无此字段。

    emit_notify=False 时静默（供 replace_position 聚合为单个 position_replaced 事件）。
    返回写入的 record。"""
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
    if setup:
        record["setup"] = str(setup)              # ADR-0001: 交易原型，candidate 继承或用户确认
    if rule_checklist:
        record["rule_checklist"] = rule_checklist  # ADR-0001: 自录 Rule 检查表，close 后守规算分
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
    # sector（行业）：前向捕获，供「我的交易系统」板块归因。tushare 行业 membership 稳定，开仓时查一次。
    sector = _sector_for(ts_code)
    if sector:
        record["sector"] = sector
    if size_method:
        record["size_method"] = str(size_method)
    data["active_positions"].append(record)
    _save(data)
    if emit_notify:
        _notify("position_opened", ts_code=ts_code, name=name,
                before=None, after=record)
    return record


def update_advice(ts_code: str,
                  stop_loss: Optional[float] = None,
                  target: Optional[float] = None,
                  calibrated_confidence: Optional[float] = None) -> dict:
    """更新已存在持仓的止损/目标/校准确信度(覆盖写)。

    用于手动持仓从最近一次 AI 分析同步 advice。总是覆盖传入的字段;
    传 None 的字段保持原值不动。

    Raises:
      PositionNotFoundError: ts_code 不在 active_positions。
    """
    data = _load()
    for pos in data["active_positions"]:
        if pos.get("ts_code") == ts_code:
            before = dict(pos)
            if stop_loss is not None:
                pos["stop_loss"] = float(stop_loss)
            if target is not None:
                pos["target"] = float(target)
            if calibrated_confidence is not None:
                pos["calibrated_confidence"] = float(calibrated_confidence)
            _save(data)
            _notify("position_advice_updated", ts_code=ts_code,
                    name=pos.get("name", ""), before=before, after=pos)
            return pos
    raise PositionNotFoundError(f"持仓 {ts_code} 不存在于 active_positions")


def update_position(ts_code: str,
                    name: Optional[str] = None,
                    stop_loss: Optional[float] = None,
                    target: Optional[float] = None,
                    trigger_price: Optional[float] = None,
                    trigger_direction: Optional[str] = None,
                    trigger_low: Optional[float] = None,
                    trigger_high: Optional[float] = None,
                    expires_at: Optional[str] = None,
                    calibrated_confidence: Optional[float] = None,
                    strategy: Optional[str] = None,
                    setup: Optional[str] = None) -> dict:
    """更新已存在持仓的交易参数(部分覆盖, None/未传不动)。

    人工干预修改: 调止损止盈 / 重挂触发价或区间 / 改过期 / strategy / setup / 名称。
    不含 entry_price/avg_cost/shares/entry_date -- 录错走 buy/sell 补录, 保 trades.jsonl 口径。

    Raises:
      PositionNotFoundError: ts_code 不在 active_positions。
      ValueError: trigger_direction 非 below/above。
    """
    if trigger_direction is not None and trigger_direction not in ("below", "above"):
        raise ValueError(f"trigger_direction 必须 below/above, got {trigger_direction}")
    data = _load()
    for pos in data["active_positions"]:
        if pos.get("ts_code") == ts_code:
            before = dict(pos)
            if name is not None:
                pos["name"] = str(name)
            if stop_loss is not None:
                pos["stop_loss"] = float(stop_loss)
            if target is not None:
                pos["target"] = float(target)
            if trigger_price is not None:
                pos["trigger_price"] = float(trigger_price)
            if trigger_direction is not None:
                pos["trigger_direction"] = trigger_direction
            if trigger_low is not None:
                pos["trigger_low"] = float(trigger_low)
            if trigger_high is not None:
                pos["trigger_high"] = float(trigger_high)
            if expires_at is not None:
                pos["expires_at"] = str(expires_at)
            if calibrated_confidence is not None:
                pos["calibrated_confidence"] = float(calibrated_confidence)
            if strategy is not None:
                pos["strategy"] = str(strategy)
            if setup is not None:
                pos["setup"] = str(setup)
            _save(data)
            _notify("position_edited", ts_code=ts_code,
                    name=pos.get("name", ""), before=before, after=pos)
            return pos
    raise PositionNotFoundError(f"持仓 {ts_code} 不存在于 active_positions")


def replace_position(ts_code: str, name: str, entry_price: float,
                     stop_loss: float, target: float,
                     trigger_price: Optional[float] = None,
                     trigger_direction: str = "below",
                     expires_days: int = 10,
                     position_size_shares: Optional[int] = None,
                     risk_amount: Optional[float] = None,
                     calibrated_confidence: Optional[float] = None,
                     strategy: Optional[str] = None,
                     regime_at_open: Optional[str] = None) -> dict:
    """Archive existing position with reason='replaced', then add new one.

    推送聚合为单个 position_replaced 事件（archive + add 都静默，避免双推）。"""
    wl = _load()
    old = next((p for p in wl["active_positions"] if p.get("ts_code") == ts_code), None)
    archive_entry(ts_code, "active_positions", reason="replaced", emit_notify=False)
    new_rec = add_position(ts_code, name, entry_price, stop_loss, target,
                           trigger_price, trigger_direction, expires_days,
                           position_size_shares=position_size_shares,
                           risk_amount=risk_amount,
                           calibrated_confidence=calibrated_confidence,
                           strategy=strategy,
                           regime_at_open=regime_at_open,
                           emit_notify=False)
    _notify("position_replaced", ts_code=ts_code, name=name,
            before=old, after=new_rec)
    return new_rec


def promote_candidate(ts_code: str, entry_price: float,
                      stop_loss: float, target: float,
                      expires_days: int = 10,
                      position_size_shares: Optional[int] = None,
                      risk_amount: Optional[float] = None,
                      calibrated_confidence: Optional[float] = None,
                      strategy: Optional[str] = None,
                      regime_at_open: Optional[str] = None,
                      setup: Optional[str] = None,
                      rule_checklist: Optional[dict] = None) -> dict:
    """Promote a candidate to active position. Archives the candidate with
    reason='promoted', then adds the active position using actual fill values.

    手数处理：未传 position_size_shares 时从最近 journal 的 ATR(14)% 推算
    (标 size_method=atr_estimated)；推算失败标 size_method=missing 且不写
    trades 流水(0 股会污染统计)。用户手填 shares 不标 size_method。
    写入一条 buy trade 到 trades.jsonl（对齐 buy() 流水口径，修断层1）。

    strategy 缺省时从候选 record 自动继承（候选时如果带了 strategy 字段）。
    """
    from apex import trades as _trades
    wl = _load()
    existing = next((p for p in wl["active_positions"] if p.get("ts_code") == ts_code), None)
    if existing is not None:
        raise DuplicatePositionError(ts_code, existing)
    cand = next((c for c in wl["candidates"] if c.get("ts_code") == ts_code), None)
    if cand is None:
        raise ValueError(f"候选 {ts_code} 不存在")
    name = cand.get("name", "")
    inherited_strategy = strategy or cand.get("strategy")
    inherited_setup = setup or cand.get("setup")  # ADR-0001: setup 也从候选继承

    # 修断层3：用户未填手数时从 journal ATR 推算兜底，避免风控/市值静默漏算
    size_method: Optional[str] = None
    resolved_shares = position_size_shares
    if not resolved_shares:
        estimated, method = _estimate_shares_from_journal(ts_code, entry_price)
        if estimated:
            resolved_shares = estimated
        size_method = method  # atr_estimated / missing

    archive_entry(ts_code, "candidates", reason="promoted")
    record = add_position(ts_code, name, entry_price, stop_loss, target,
                 trigger_price=None, trigger_direction="below",
                 expires_days=expires_days,
                 position_size_shares=resolved_shares,
                 risk_amount=risk_amount,
                 calibrated_confidence=calibrated_confidence,
                 strategy=inherited_strategy,
                 regime_at_open=regime_at_open,
                 setup=inherited_setup,
                 rule_checklist=rule_checklist,
                 size_method=size_method)

    # 修断层1：promote 也写 buy trade，对齐 buy() 流水口径；
    # 缺 shares（推算失败）则跳过——0 股会污染 trade_history/未来诊断统计
    if resolved_shares and resolved_shares > 0:
        _trades.append_trade(
            ts_code=ts_code, name=name, side="buy",
            fill_price=entry_price, shares=resolved_shares,
            avg_cost_after=entry_price, shares_after=resolved_shares,
            strategy=inherited_strategy, regime=regime_at_open,
            journal_ref=_journal_ref_for(ts_code), realized_pnl=None,
            note="promoted from candidate",
        )
    return record


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


def _estimate_shares_from_journal(ts_code: str, entry_price: float) -> tuple[Optional[int], str]:
    """从最近 journal 的 ATR(14)% 推算建议手数。零 API 调用（复用已存 journal）。

    返回 (shares, method)：
      - 推算成功 → (shares, "atr_estimated")
      - 无 journal / 无 ATR / 风险预算不足 → (None, "missing")

    用于 promote_candidate 用户未填 position_size_shares 时的兜底，
    避免持仓缺手数导致 account.current_total_risk / summary 静默漏算。
    """
    try:
        from apex import journal as _journal, account as _account
        latest = _journal.load_latest(ts_code)
        if not latest:
            return None, "missing"
        atr_pct = (latest.get("features") or {}).get("atr_14_pct")
        if not atr_pct or float(atr_pct) <= 0:
            return None, "missing"
        ps = _account.compute_position_size_atr(
            entry=float(entry_price), atr_pct=float(atr_pct),
        )
        if not ps.get("ok") or not ps.get("shares"):
            return None, "missing"
        return int(ps["shares"]), "atr_estimated"
    except Exception:
        return None, "missing"


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
    # 修断层2：绕过 promote 直接 buy 时，归档同 code 候选避免重复触发/推送/成交
    if any(c.get("ts_code") == ts_code for c in wl.get("candidates", [])):
        archive_entry(ts_code, "candidates", reason="bought_directly", emit_notify=False)
        wl = _load()  # archive_entry 已 _save，重新加载
    existing = next((p for p in wl["active_positions"] if p.get("ts_code") == ts_code), None)

    regime_label = regime if regime is not None else _today_regime()
    journal_ref = _journal_ref_for(ts_code)

    if existing is not None:
        # 加仓
        before_snapshot = dict(existing)
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
        before_snapshot = None

    _save(wl)
    trade = _trades.append_trade(
        ts_code=ts_code, name=position.get("name", ""),
        side="buy", fill_price=fill_price, shares=shares,
        avg_cost_after=position.get("avg_cost"), shares_after=position.get("position_size_shares"),
        strategy=position.get("strategy") or strategy, regime=regime_label,
        journal_ref=journal_ref, realized_pnl=None, note=note,
    )
    _notify("position_opened" if existing is None else "position_increased",
            ts_code=ts_code, name=position.get("name", ""),
            before=before_snapshot, after=position, trade=trade)
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
        before = dict(pos)
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
            record_trade=False,  # 上面已 append_trade, 避免双写；推送也由本分支发出，避免双推
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
        _notify("position_closed", ts_code=ts_code, name=name,
                before=before, after=None, trade=trade,
                close=closed_record.get("close"), exit_reason=exit_reason)
        return {"trade": trade, "closed_record": closed_record, "diagnosis": diagnosis}

    # 减仓 → 留痕 + 改状态, 不复盘
    before = dict(pos)
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
    _notify("position_decreased", ts_code=ts_code, name=name,
            before=before, after=pos, trade=trade, exit_reason=exit_reason)
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


def dedup_candidates() -> int:
    """
    Collapse duplicate ts_codes in candidates: keep the one with the latest
    expires_at (tiebreak: keep last in list), archive the rest with
    reason='dedup'. Returns count archived. 对齐 dedup_active_positions 语义。
    """
    wl = _load()
    today = date.today().isoformat()
    seen: dict[str, dict] = {}
    no_code: list[dict] = []
    archived_count = 0
    for item in wl["candidates"]:
        code = item.get("ts_code")
        if not code:
            no_code.append(item)
            continue
        if code not in seen:
            seen[code] = item
            continue
        prev = seen[code]
        if (item.get("expires_at") or "") >= (prev.get("expires_at") or ""):
            loser, winner = prev, item
        else:
            loser, winner = item, prev
        wl["archived"].append({
            **loser, "status": "archived_dedup",
            "archived_date": today, "archived_from": "candidates",
        })
        seen[code] = winner
        archived_count += 1
    if archived_count:
        wl["candidates"] = no_code + list(seen.values())
        _save(wl)
    return archived_count


def archive_entry(ts_code: str, section: Optional[str] = None, reason: str = "manual",
                  emit_notify: bool = True) -> bool:
    """Move an entry from active_positions or candidates into archived. Returns True if moved.

    section 缺省时自动探测: 候选优先, 再 active_positions。便于前端只传 ts_code+reason。
    emit_notify=False 时静默（供 replace_position 聚合事件）。仅 active_positions 归档触发推送。
    """
    wl = _load()
    if section is None:
        for s in ("candidates", "active_positions"):
            if any(item.get("ts_code") == ts_code for item in wl.get(s, [])):
                section = s
                break
    if section not in ("active_positions", "candidates"):
        return False
    today = date.today().isoformat()
    for i, item in enumerate(wl[section]):
        if item.get("ts_code") == ts_code:
            before = dict(item)
            item["status"] = f"archived_{reason}"
            item["archived_date"] = today
            item["archived_from"] = section
            wl["archived"].append(item)
            wl[section].pop(i)
            _save(wl)
            if section == "active_positions" and emit_notify:
                _notify("position_archived", ts_code=ts_code,
                        name=item.get("name", ""), before=before, after=None)
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
            # 用 analyzed_at（canonical）比对；before 是日期则补到当天 23:59:59 以纳入同日分析
            cutoff = before if "T" in before else before + "T23:59:59"
            entries = [e for e in entries if (e.get("analyzed_at") or e.get("date") or "") <= cutoff]
        if not entries:
            return None
        return sorted(entries, key=lambda e: e.get("analyzed_at") or e.get("date", ""))[-1]
    except Exception:
        return None


def _sector_for(ts_code: str) -> Optional[str]:
    """ts_code -> tushare 行业（industry，point-in-time 基本稳定）。失败/缺省返回 None。"""
    if not ts_code:
        return None
    from apex import data as _data
    try:
        info_raw = _data.get_stock_info(ts_code=ts_code)
        info_list = json.loads(info_raw)
        if isinstance(info_list, list) and info_list:
            ind = info_list[0].get("industry")
            if ind:
                return str(ind)
    except Exception:
        pass
    return None


def _backfill_closed_open_fields() -> dict:
    """PR4: 回填 closed_positions.jsonl 里 open 块缺失字段。

    - regime_at_open：只从缓存读（regime.load(entry_date)），不主动 collect 历史。
    - sector：tushare 行业（membership 稳定）。
    - ai_price_advice：开仓时 AI 计划快照（与 close 时同源 _latest_journal_for），
      让 system 层读快照而非重新查找（消除 look-ahead + 口径不一）。
    只填 null，绝不覆盖已有值；原子写（temp + os.replace）。
    返回 {regime_filled, sector_filled, price_advice_filled, rewritten}。"""
    import os
    import tempfile
    from apex import regime as _regime_mod

    path = _closed_positions_path()
    if not path.exists():
        return {"regime_filled": 0, "sector_filled": 0,
                "price_advice_filled": 0, "rewritten": False}

    rows: list[str] = []
    regime_filled = sector_filled = price_advice_filled = 0
    rewritten = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                rows.append(raw)  # 坏行原样保留
                continue
            op = rec.get("open") or {}
            changed = False
            if not op.get("regime_at_open"):
                ed = op.get("entry_date") or ""
                try:
                    r = _regime_mod.load(ed) if ed else None
                except Exception:
                    r = None
                if r and r.get("label"):
                    op["regime_at_open"] = r["label"]
                    regime_filled += 1
                    changed = True
            if not op.get("sector"):
                ind = _sector_for(rec.get("ts_code", ""))
                if ind:
                    op["sector"] = ind
                    sector_filled += 1
                    changed = True
            if not op.get("ai_price_advice"):
                # 老记录无烘焙的 AI 计划快照 -> 用入场前最近分析补（与 close 时同源）
                try:
                    jo = _latest_journal_for(rec.get("ts_code", ""), before=op.get("entry_date"))
                except Exception:
                    jo = None
                pa = (jo or {}).get("price_advice")
                if pa:
                    op["ai_price_advice"] = pa
                    price_advice_filled += 1
                    changed = True
            if changed:
                rec["open"] = op
                rewritten = True
                rows.append(json.dumps(rec, ensure_ascii=False, default=str))
            else:
                rows.append(raw)
    if rewritten:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(rows) + "\n")
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return {"regime_filled": regime_filled, "sector_filled": sector_filled,
            "price_advice_filled": price_advice_filled, "rewritten": rewritten}


def _build_reconstructed_closed(ts_code: str, buys: list, sells: list) -> Optional[dict]:
    """把一组完整 round-trip 的 trades 合成一条 closed 记录（quality-flagged）。

    入场=首笔 buy（价/日）；出场=末笔 sell（价/日）；avg_cost=加权平均买入价；
    realized_pnl_pct=(exit-avg_cost)/avg_cost；shares=总买入股数。
    high/low/AI 特征/止损/目标全 None（无法干净重建）-> source/quality 标低质量。
    """
    if not buys or not sells:
        return None
    first_buy = buys[0]
    last_sell = sells[-1]
    entry_date = (first_buy.get("traded_at") or "")[:10] or None
    exit_date = (last_sell.get("traded_at") or "")[:10] or None
    buy_shares = sum(int(t.get("shares") or 0) for t in buys)
    buy_amount = sum(float(t.get("fill_price") or 0) * int(t.get("shares") or 0) for t in buys)
    avg_cost = round(buy_amount / buy_shares, 4) if buy_shares else None
    exit_price = float(last_sell.get("fill_price") or 0)
    realized_pnl_pct = None
    realized_pnl_amount = None
    if avg_cost and avg_cost > 0 and exit_price > 0:
        realized_pnl_pct = round((exit_price - avg_cost) / avg_cost, 4)
        realized_pnl_amount = round((exit_price - avg_cost) * buy_shares, 2)
    # trades 里每笔 sell 已带 realized_pnl，取末笔兜底（与 closed record 单值口径近似）
    sell_pnls = [float(t.get("realized_pnl") or 0) for t in sells if t.get("realized_pnl") is not None]
    realized_pnl_sum = round(sum(sell_pnls), 2) if sell_pnls else None

    days_held = 0
    try:
        if entry_date and exit_date:
            days_held = max(0, (date.fromisoformat(exit_date) - date.fromisoformat(entry_date)).days)
    except Exception:
        pass

    return {
        "ts_code": ts_code,
        "name": first_buy.get("name") or "",
        "source": "reconstructed_from_trades",
        "quality": "low",   # 缺 AI 特征 / 止损目标 / 期间高低点
        "open": {
            "entry_date": entry_date,
            "entry_price": avg_cost,
            "avg_cost": avg_cost,
            "actual_fill_price": float(first_buy.get("fill_price") or 0) or None,
            "position_size_shares": buy_shares or None,
            "stop_loss": None, "target": None,
            "risk_amount": None, "calibrated_confidence": None,
            "strategy": first_buy.get("strategy"),
            "regime_at_open": None, "setup": None, "rule_checklist": None,
            "sector": _sector_for(ts_code),
            "ai_verdict": None, "ai_confidence": None, "ai_features": None, "ai_analysis_text": None,
            "screener_signals": None, "screener_rule_score": None,
        },
        "close": {
            "exit_date": exit_date,
            "actual_exit_price": exit_price or None,
            "exit_reason": "manual",   # 重建无法判定真实出场原因，标 manual + source 区分
            "days_held": days_held,
            "trading_days_held": None,
            "high_during_hold": None, "low_during_hold": None,
            "realized_pnl_pct": realized_pnl_pct,
            "realized_pnl_amount": realized_pnl_amount or realized_pnl_sum,
            "user_notes": "从 trades.jsonl 重建（无 AI 特征/止损目标/期间高低点）",
            "closed_at": last_sell.get("traded_at") or datetime.now(_TZ_CN).isoformat(timespec="seconds"),
        },
        "diagnosis": None,
    }


def _reconstruct_closed_from_trades() -> dict:
    """PR4: 从 trades.jsonl 重建「完整平仓」记录补进 closed_positions.jsonl。

    只重建「累计卖出 == 累计买入」的完整 round-trip；部分平仓（仍持仓）不重建
    （其已实现盈亏留在 trades.jsonl 供行为分析用）。已存在同 ts_code closed 记录的
    跳过（保守去重，v1 不支持同股多次独立 round-trip 重建）。重建记录带
    source='reconstructed_from_trades' + quality='low'。幂等：重复运行不会追加重复记录。
    返回 {reconstructed, skipped_already_closed, skipped_partial, open_only}。"""
    from collections import defaultdict
    from apex import trades as _trades

    path = _closed_positions_path()
    existing_codes: set[str] = set()
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("ts_code"):
                    existing_codes.add(rec["ts_code"])

    by_code: dict[str, list] = defaultdict(list)
    for t in _trades.load_trades():
        by_code[t.get("ts_code")].append(t)

    reconstructed = skipped_already = skipped_partial = open_only = 0
    new_records: list[dict] = []
    for code, tlst in by_code.items():
        tlst = sorted(tlst, key=lambda t: t.get("traded_at") or "")
        buy_shares = sum(int(t.get("shares") or 0) for t in tlst if t.get("side") == "buy")
        sell_shares = sum(int(t.get("shares") or 0) for t in tlst if t.get("side") == "sell")
        if sell_shares == 0:
            open_only += 1
            continue
        if sell_shares < buy_shares:
            skipped_partial += 1
            continue
        if code in existing_codes:
            skipped_already += 1
            continue
        rec = _build_reconstructed_closed(
            code,
            [t for t in tlst if t.get("side") == "buy"],
            [t for t in tlst if t.get("side") == "sell"],
        )
        if rec:
            new_records.append(rec)
            reconstructed += 1

    if new_records:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for rec in new_records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    return {
        "reconstructed": reconstructed,
        "skipped_already_closed": skipped_already,
        "skipped_partial": skipped_partial,
        "open_only": open_only,
    }


def _eval_one_rule(key: str, *, fill_price: Optional[float], stop_loss,
                   ai_entry, ai_low, ai_high, ai_features: dict,
                   buys_in_hold: list) -> Optional[bool]:
    """单条 Rule 客观评估。True=遵守 / False=违规 / None=无法客观判定（留自评）。

    可客观评估（无需用户参数）：entry_band / stop_formula / no_average_down / no_chase。
    需参数的（sizing_cap / max_hold_days / sector_conc_cap）一律返回 None，待用户自评。"""
    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    if key == "entry_band":
        # 入场带：fill 落在 AI plan [entry_low, entry_high] 内；无带则 ±2% of entry
        fp = _f(fill_price)
        if fp is None or fp <= 0:
            return None
        lo, hi = _f(ai_low), _f(ai_high)
        if lo is not None and hi is not None:
            return lo <= fp <= hi
        ae = _f(ai_entry)
        if ae is not None and ae > 0:
            return abs(fp - ae) / ae <= 0.02
        return None  # 无 AI plan，无法判定

    if key == "stop_formula":
        # 止损按规则设置：stop_loss 存在且 > 0（承诺设止损却没设 = 违规）
        s = _f(stop_loss)
        return bool(s is not None and s > 0)

    if key == "no_average_down":
        # 不加仓于亏损：持仓期间多笔 buy 且后续价 < 前笔 = 违规
        if len(buys_in_hold) <= 1:
            return True
        for i in range(1, len(buys_in_hold)):
            prev = _f(buys_in_hold[i - 1].get("fill_price"))
            cur = _f(buys_in_hold[i].get("fill_price"))
            if prev is not None and cur is not None and cur < prev:
                return False
        return True

    if key == "no_chase":
        # 不追高：fill <= ai_entry * 1.05（与 fomo 代理同阈值）；无 AI plan 用特征代理
        fp = _f(fill_price)
        ae = _f(ai_entry)
        if fp is not None and ae is not None and ae > 0:
            return fp <= ae * 1.05
        pos = (ai_features or {}).get("ma5_position")
        pvma = _f((ai_features or {}).get("price_vs_ma5_pct"))
        if pos is not None and pvma is not None:
            return not (pos == "above" and pvma > 3)
        return None

    # sizing_cap / max_hold_days / sector_conc_cap：需用户参数，无法客观判定
    return None


def _evaluate_rule_checklist(rule: dict, *, ts_code: str, fill_price: Optional[float],
                             stop_loss, ai_plan: dict, ai_features: dict,
                             entry_date: str, exit_date: str) -> dict:
    """close 时评估自录 Rule 检查表：可客观评估的项设 checked=True/False，
    需参数的项留 null（待自评）。已有 checked 值不覆盖（尊重用户自评）。
    返回更新后的 rule dict（原地改 items）。"""
    items = rule.get("items") or []
    if not items:
        return rule

    buys_in_hold: list = []
    try:
        from apex import trades as _trades
        tlst = sorted(_trades.load_trades(ts_code=ts_code),
                      key=lambda t: t.get("traded_at") or "")
        for t in tlst:
            ta = (t.get("traded_at") or "")[:10]
            if t.get("side") == "buy" and ta and entry_date <= ta <= exit_date:
                buys_in_hold.append(t)
    except Exception:
        pass

    ai_entry = ai_plan.get("entry")
    ai_low = ai_plan.get("entry_low")
    ai_high = ai_plan.get("entry_high")

    for item in items:
        if item.get("checked") is not None:
            continue
        val = _eval_one_rule(item.get("key"), fill_price=fill_price, stop_loss=stop_loss,
                             ai_entry=ai_entry, ai_low=ai_low, ai_high=ai_high,
                             ai_features=ai_features, buys_in_hold=buys_in_hold)
        if val is not None:
            item["checked"] = val
    return rule


class PositionNotFoundError(Exception):
    pass


def close_position(ts_code: str,
                   exit_price: float,
                   exit_reason: str = "manual",
                   exit_date: Optional[str] = None,
                   user_notes: str = "",
                   actual_fill_price: Optional[float] = None,
                   record_trade: bool = True) -> dict:
    """平仓一笔持仓 → 写一条完整记录到 closed_positions.jsonl，并从 active_positions 移除。

    返回写入的 closed record（不含 diagnosis 段，那是 1.4 post-mortem 的活）。

    actual_fill_price: 开仓时的实际成交价（A股可能与 entry_price 略有滑点）；缺省回落到 entry_price。
    record_trade: 是否追加一条 sell trade 到 trades.jsonl。直接走 /close 的平仓默认 True；
                  sell() 卖光分支已自行留痕, 会传 False 避免双写。

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

    # ADR-0001: close 时评估自录 Rule 检查表（承诺 -> 客观判定遵守/违规）
    evaluated_rule = pos.get("rule_checklist")
    if evaluated_rule and evaluated_rule.get("items"):
        evaluated_rule = _evaluate_rule_checklist(
            evaluated_rule, ts_code=ts_code, fill_price=fill_price,
            stop_loss=pos.get("stop_loss"),
            ai_plan=(journal_at_open or {}).get("price_advice") or {},
            ai_features=(journal_at_open or {}).get("features") or {},
            entry_date=entry_date, exit_date=exit_date,
        )

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
            "setup": pos.get("setup"),
            "rule_checklist": evaluated_rule,
            "sector": pos.get("sector") or _sector_for(ts_code),
            "ai_verdict": (journal_at_open or {}).get("verdict"),
            "ai_confidence": (journal_at_open or {}).get("confidence"),
            "ai_features": (journal_at_open or {}).get("features"),
            "ai_analysis_text": (journal_at_open or {}).get("analysis_text"),
            "ai_price_advice": (journal_at_open or {}).get("price_advice"),
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

    # 平仓即卖光: 追加一条 sell trade 留痕(realized_pnl 与 closed record 同口径, 基准=fill_price)。
    # sell() 卖光分支已自行 append_trade, 走 record_trade=False 避免双写。
    # 推送同理: 仅直接调用 close_position(POST /close) 时在此推 position_closed；
    # sell() 卖光分支自行推送, 传 record_trade=False 跳过此处避免双推。
    if record_trade:
        from apex import trades as _trades
        sell_trade = _trades.append_trade(
            ts_code=ts_code, name=pos.get("name", ""), side="sell",
            fill_price=float(exit_price), shares=shares,
            avg_cost_after=None, shares_after=0,
            strategy=pos.get("strategy"), regime=_today_regime(),
            journal_ref=_journal_ref_for(ts_code),
            realized_pnl=realized_pnl_amount, realized_pnl_pct=realized_pnl_pct,
            note=user_notes or exit_reason,
        )
        _notify("position_closed", ts_code=ts_code, name=pos.get("name", ""),
                before=dict(pos), after=None, trade=sell_trade,
                close=record.get("close"), exit_reason=exit_reason)

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


def _dynamic_expiry_days(ts_code: str, trigger_price: float) -> tuple[int, dict]:
    """基于波动率衰减算候选过期天数。

    随机游走: 价格 t 天后扩散 σ√t。当 σ√t 追上"到触发价距离 d"时,
    纯噪声就能把价格漂到触发价 → 触发不再携带原分析信号。临界 t* = (d/σ)²。
    σ 用近 20 日日收益率标准差; t* 严格是交易日数, ×7/5 换算日历天。
    任何异常/数据不足 → fallback 7 天(原硬编码默认, 保持向后兼容)。
    """
    from apex import data as _data
    from apex.technical import realized_vol
    FLOOR, CEILING = 3, 30
    try:
        raw = _data.get_daily_price(ts_code, adj="qfq")
        bars = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(bars, list) or len(bars) < 2:
            raise ValueError("insufficient bars")
        sigma = realized_vol(bars, window=20)
        if not sigma or sigma <= 0:
            raise ValueError("no vol")
        last_close = float(bars[-1]["close"])
        if trigger_price <= 0:
            raise ValueError("bad trigger")
        distance = abs(last_close - trigger_price) / trigger_price
        t_trading = (distance / sigma) ** 2
        days = round(t_trading * 7 / 5)  # 交易日 → 日历天
        days = max(FLOOR, min(CEILING, days))
        meta = {
            "method": "vol_based",
            "realized_vol": round(sigma, 6),
            "distance_pct": round(distance * 100, 2),
            "t_trading": round(t_trading, 2),
        }
        return days, meta
    except Exception:
        return 7, {"method": "fallback"}


def add_candidate(ts_code: str, name: str, trigger_price: float,
                  trigger_direction: str = "above",
                  note: str = "",
                  expires_days: Optional[int] = None,
                  stop_advice: Optional[float] = None,
                  target_advice: Optional[float] = None,
                  strategy: Optional[str] = None,
                  setup: Optional[str] = None,
                  trigger_low: Optional[float] = None,
                  trigger_high: Optional[float] = None) -> dict:
    from datetime import timedelta
    data = _load()
    if expires_days is None:
        resolved, meta = _dynamic_expiry_days(ts_code, trigger_price)
    else:
        resolved, meta = expires_days, {"method": "manual"}
    expires = (date.today() + timedelta(days=resolved)).isoformat()
    entry: dict = {
        "ts_code": ts_code,
        "name": name,
        "trigger_price": trigger_price,
        "trigger_direction": trigger_direction,
        "expires_at": expires,
        "expires_meta": {"expires_days": resolved, **meta},
        "note": note,
    }
    if stop_advice is not None:
        entry["stop_advice"] = stop_advice
    if target_advice is not None:
        entry["target_advice"] = target_advice
    if strategy:
        entry["strategy"] = str(strategy)
    if setup:
        entry["setup"] = str(setup)   # ADR-0001: 交易原型，可由 AI verdict setup_tag 预填
    # 买入区间（带状触发）：有则 is_triggered 走 low<=price<=high，无则退回单向阈值。
    if trigger_low is not None:
        entry["trigger_low"] = float(trigger_low)
    if trigger_high is not None:
        entry["trigger_high"] = float(trigger_high)
    # upsert: 同 ts_code 已有候选则原地替换（保留 renew_count 不丢手动续期历史），
    # 否则 append。避免 candidates 出现重复 ts_code -> 前端 key 冲突 + 触发歧义。
    renew_count = None
    existing_idx = None
    for i, c in enumerate(data["candidates"]):
        if c.get("ts_code") == ts_code:
            existing_idx = i
            renew_count = c.get("renew_count")
            break
    if renew_count is not None:
        entry["renew_count"] = renew_count
    if existing_idx is not None:
        data["candidates"][existing_idx] = entry
    else:
        data["candidates"].append(entry)
    _save(data)
    return {"expires_days": resolved, **meta}


def update_candidate(ts_code: str,
                     name: Optional[str] = None,
                     trigger_price: Optional[float] = None,
                     trigger_direction: Optional[str] = None,
                     trigger_low: Optional[float] = None,
                     trigger_high: Optional[float] = None,
                     stop_advice: Optional[float] = None,
                     target_advice: Optional[float] = None,
                     note: Optional[str] = None,
                     expires_at: Optional[str] = None,
                     strategy: Optional[str] = None,
                     setup: Optional[str] = None) -> dict:
    """更新已存在候选的交易参数(部分覆盖, None/未传不动)。不调 AI, 不推送。

    人工干预修改: 调触发价/区间/方向 / 止损建议 / 目标建议 / 备注 / 过期 / strategy / setup / 名称。
    改 trigger_price 不自动重算 expires_at(手动编辑=精确控制; 续期用 renew, 跟AI用 sync-ai)。

    Raises:
      ValueError: 候选不存在 / trigger_direction 非 below/above。
    """
    if trigger_direction is not None and trigger_direction not in ("below", "above"):
        raise ValueError(f"trigger_direction 必须 below/above, got {trigger_direction}")
    data = _load()
    for item in data["candidates"]:
        if item.get("ts_code") == ts_code:
            if name is not None:
                item["name"] = str(name)
            if trigger_price is not None:
                item["trigger_price"] = float(trigger_price)
            if trigger_direction is not None:
                item["trigger_direction"] = trigger_direction
            if trigger_low is not None:
                item["trigger_low"] = float(trigger_low)
            if trigger_high is not None:
                item["trigger_high"] = float(trigger_high)
            if stop_advice is not None:
                item["stop_advice"] = float(stop_advice)
            if target_advice is not None:
                item["target_advice"] = float(target_advice)
            if note is not None:
                item["note"] = str(note)
            if expires_at is not None:
                item["expires_at"] = str(expires_at)
            if strategy is not None:
                item["strategy"] = str(strategy)
            if setup is not None:
                item["setup"] = str(setup)
            _save(data)
            return item
    raise ValueError(f"候选 {ts_code} 不存在")


def sync_candidate_from_journal(ts_code: str) -> Optional[dict]:
    """从最近一次 AI 分析同步候选的价位: entry→trigger_price / stop_loss→stop_advice /
    target→target_advice。trigger 变了, 用新 trigger 重算过期天数(距离变了)。

    不调 AI(零成本), 只复用已有 journal。journal 无分析或无 price_advice → None。
    返回同步后的字段 + 续期天数(因 trigger 变了距离也变)。
    隐式延长(新 expires_at 晚于旧值)时 renew_count++, 与 renew_candidate 僵尸防护闭环;
    缩短不计数。返回带 renewed 标记本次是否触发续期。
    """
    from apex import journal
    latest = journal.load_latest(ts_code)
    if not latest:
        return None
    pa = latest.get("price_advice") or {}
    entry = pa.get("entry")
    stop = pa.get("stop_loss")
    target = pa.get("target")
    if entry is None or (entry is not None and float(entry) <= 0):
        return None  # entry 缺/无效, 不同步(触发价不能空)

    data = _load()
    for item in data["candidates"]:
        if item.get("ts_code") != ts_code:
            continue
        # 三字段全覆盖
        item["trigger_price"] = float(entry)
        if stop is not None and float(stop) > 0:
            item["stop_advice"] = float(stop)
        if target is not None and float(target) > 0:
            item["target_advice"] = float(target)
        # trigger 变了 → 距离变了 → 重算过期(用新 entry 距离今天的波动)
        resolved, meta = _dynamic_expiry_days(ts_code, float(entry))
        from datetime import timedelta
        old_expires = item.get("expires_at")
        new_expires = (date.today() + timedelta(days=resolved)).isoformat()
        item["expires_at"] = new_expires
        item["expires_meta"] = {"expires_days": resolved, **meta}
        # 隐式延长才计 renew_count(缩短不算续期), 与 renew_candidate 僵尸防护闭环
        renewed = bool(old_expires and new_expires > old_expires)
        if renewed:
            item["renew_count"] = item.get("renew_count", 0) + 1
        _save(data)
        return {
            "trigger_price": float(entry),
            "stop_advice": item.get("stop_advice"),
            "target_advice": item.get("target_advice"),
            "analyzed_at": latest.get("analyzed_at") or latest.get("date"),
            "expires_days": resolved,
            "renew_count": item.get("renew_count", 0),
            "renewed": renewed,
            **meta,
        }
    return None


def renew_candidate(ts_code: str) -> Optional[dict]:
    """续期一个已过期的候选: trigger 不动, 用今天的波动率 + 原 trigger 距离重算过期天数。

    不调 AI(零成本), 纯粋延长观察期。代价是可能养出僵尸候选(多次续期),
    所以前端续期时应提示"已续 X 次"提醒用户。
    返回更新后的 meta, 找不到候选返回 None。
    """
    from datetime import timedelta
    data = _load()
    for item in data["candidates"]:
        if item.get("ts_code") != ts_code:
            continue
        trigger_price = item.get("trigger_price")
        if not trigger_price or trigger_price <= 0:
            return None
        resolved, meta = _dynamic_expiry_days(ts_code, float(trigger_price))
        item["expires_at"] = (date.today() + timedelta(days=resolved)).isoformat()
        item["expires_meta"] = {"expires_days": resolved, **meta}
        renew_count = item.get("renew_count", 0) + 1
        item["renew_count"] = renew_count
        _save(data)
        return {"expires_days": resolved, **meta, "renew_count": renew_count}
    return None
