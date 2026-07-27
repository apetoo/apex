"""交易流水审计:每笔买入/卖出追加一行到 ~/.stock-journal/trades.jsonl。

留痕供未来 AI 操作诊断(买卖时机/加仓减仓节奏)。adherence 字段在写入时回查最近
position_action 做加减仓守规比对(纯规则,零 AI);其余字段只写不分析。
字段形状见 docs/superpowers/specs/2026-06-27-manual-positions-trades-design.md。
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TypedDict

from apex import config, journal
from apex.evidence_attribution import _MATCH_WINDOW_DAYS as _ADHERENCE_STALE_DAYS

_TZ_CN = timezone(timedelta(hours=8))


def _path() -> Path:
    cfg = config.get()
    journal_dir = Path(cfg["paths"]["journal_dir"]).expanduser()
    return journal_dir / "trades.jsonl"


def _next_trade_id(now: datetime) -> str:
    """YYYYMMDDTHHMMSS-<4位序号>。同秒多笔按文件内已有同秒计数 +1。"""
    stamp = now.strftime("%Y%m%dT%H%M%S")
    seq = 0
    p = _path()
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (rec.get("trade_id") or "").startswith(stamp):
                    seq += 1
    return f"{stamp}-{seq:04d}"


# ---- 加减仓 adherence 事后比对（design: wanmingyu-develop-design-20260727-001422）----
# 每笔 buy/sell 回查最近 position_action，按 premise 1 矩阵判"跟没跟"。纯规则、零 AI。
# D2 下沉：append_trade 内部调（单一入口覆盖 buy/sell/close_position 三调用点）。
# D3 best-effort：任何异常返回 None，不阻断 trade 写入。
# D6 scale_plan：hold 带 scale_plan 时按 fill_price 比对 add/trim trigger（headline action 不够）。
# D7 cycle 过滤：只取 entry_date <= analyzed_at <= when 的建议，排除上周期 position_action。

class Adherence(TypedDict, total=False):
    """单笔 trade 与最近 position_action 的事后守规比对（加减仓维度）。

    followed 三态：True=跟建议 / False=偏离 / None=不入统计（na）。
    na_reason 仅在 followed is None 时出现，区分两种 na（spec premise 1 命名区分但 followed 合并，
    此字段补全；Open Q3 的 freshness gate 只作用于 na_hold_advice，需可区分）。
    """
    action_advice: str                   # hold/add/trim/exit（命中 position_action 的 headline action）
    followed: Optional[bool]             # True=follow, False=deviate, None=na_hold_advice/na_no_trigger
    partial: bool                        # action=exit 且部分减仓（未清仓）
    stale: bool                          # analyzed_at 距 when > _ADHERENCE_STALE_DAYS
    matched_action_at: str               # 命中 position_action.analyzed_at，审计用
    matched_scale_level: Optional[dict]  # 命中的 scale_plan level（hold+scale_plan 命中时），审计用
    na_reason: Optional[str]             # "na_hold_advice" | "na_no_trigger"（followed is None 时）
    source: str                          # "realtime"（trade 时算，非回填；D4 删 backfill）


def _parse_dt(value) -> Optional[datetime]:
    """宽松解析 datetime / ISO 串为 CN 时区 aware datetime。

    date-only 串（entry_date "2026-07-27"）按当日 00:00 CN；带时区全 ISO 串保留原时区。
    None / 无法解析返回 None。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_TZ_CN)
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])  # 兜底 date-only 前缀
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_CN)
    return dt


def _eval_hold_scale_plan(scale_plan, side: str, fill_price: float) -> tuple[Optional[bool], Optional[dict], Optional[str]]:
    """hold + scale_plan 的 trigger 命中评估（D6）。

    返回 (followed, matched_scale_level, na_reason)：
      - scale_plan 空/缺           -> (None, None, "na_hold_advice")
      - buy 命中 add level          -> (True, level, None)   fill_price >= trigger
      - sell 命中 trim level        -> (True, level, None)   fill_price <= trigger
      - 非空但未命中 / 格式异常     -> (None, None, "na_no_trigger")
    多档命中取最贴近 fill_price 的一档（add 取 max trigger，trim 取 min trigger）。
    格式异常（trigger_price 非数等）走 except 当 na_no_trigger（failure mode 表）。
    """
    if not scale_plan:
        return None, None, "na_hold_advice"
    try:
        fp = float(fill_price)
        want = "add" if side == "buy" else "trim"
        cands: list[tuple[float, dict]] = []
        for lvl in scale_plan:
            if not isinstance(lvl, dict) or lvl.get("action") != want:
                continue
            tp = lvl.get("trigger_price")
            if tp is None:
                continue
            tp = float(tp)
            if (side == "buy" and fp >= tp) or (side == "sell" and fp <= tp):
                cands.append((tp, lvl))
        if not cands:
            return None, None, "na_no_trigger"
        # add 取 max trigger（最贴近 fill 从下），trim 取 min trigger（最贴近 fill 从上）
        cands.sort(key=lambda x: x[0], reverse=(side == "buy"))
        return True, cands[0][1], None
    except (TypeError, ValueError):
        return None, None, "na_no_trigger"


def _adherence_for(*,
                   ts_code: str,
                   side: str,
                   fill_price: float,
                   when,
                   shares_after: Optional[int],
                   entry_date: Optional[str]) -> Optional[Adherence]:
    """回查最近 position_action，按 premise 1 矩阵判定本笔 trade 的加减仓守规。

    D7 cycle 过滤：只取 entry_date <= analyzed_at <= when 的 position_action（排除上周期建议），
    取满足条件的最近一条。D6 scale_plan：hold 带 scale_plan 时按 fill_price 比对 trigger。
    D3 best-effort：任何异常返回 None，不阻断 trade 写入。

    无 position_action（或全在 entry_date 之前 / 开仓 buy 无 entry_date）返回 None。
    """
    try:
        # 开仓 buy 无 entry_date -> 无 position_action 可比（入场 verdict 不是加减仓建议）
        if entry_date is None:
            return None
        when_dt = _parse_dt(when)
        entry_dt = _parse_dt(entry_date)
        if when_dt is None or entry_dt is None:
            return None

        # D7 cycle 过滤：entry_date <= analyzed_at <= when，取最近一条（不依赖文件顺序，显式排序）
        candidates: list[tuple[datetime, dict]] = []
        for pa in journal.load_position_actions(ts_code):
            pa_dt = _parse_dt(pa.get("analyzed_at") or pa.get("date"))
            if pa_dt is None:
                continue
            if entry_dt <= pa_dt <= when_dt:
                candidates.append((pa_dt, pa))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        matched_dt, matched = candidates[-1]

        stale = (when_dt - matched_dt) > timedelta(days=_ADHERENCE_STALE_DAYS)

        pa_block = matched.get("position_action") or {}
        action_advice = pa_block.get("action") or "hold"
        scale_plan = pa_block.get("scale_plan") or []

        followed: Optional[bool] = None
        partial = False
        matched_level: Optional[dict] = None
        na_reason: Optional[str] = None

        if action_advice == "add":
            followed = (side == "buy")
        elif action_advice == "trim":
            followed = (side == "sell")
        elif action_advice == "exit":
            if side == "sell":
                # shares_after == 0 = 清仓（follow）；>0 或未知 = 部分减仓（deviate + partial）
                if shares_after == 0:
                    followed = True
                else:
                    followed = False
                    partial = True
            else:  # buy 偏离 exit 建议
                followed = False
        else:  # hold（含未知 action 兜底为 hold）
            followed, matched_level, na_reason = _eval_hold_scale_plan(scale_plan, side, fill_price)

        out: Adherence = {
            "action_advice": action_advice,
            "followed": followed,
            "stale": stale,
            "matched_action_at": matched.get("analyzed_at") or matched.get("date") or "",
            "matched_scale_level": matched_level,
            "source": "realtime",
        }
        if partial:
            out["partial"] = True
        if na_reason is not None:
            out["na_reason"] = na_reason
        return out
    except Exception:
        # D3 best-effort：journal IO / 解析异常 -> None，trade 照常写入（failure mode 表）
        return None


def append_trade(*,
                 ts_code: str,
                 name: str,
                 side: str,
                 fill_price: float,
                 shares: int,
                 avg_cost_after: Optional[float],
                 shares_after: Optional[int],
                 strategy: Optional[str] = None,
                 regime: Optional[str] = None,
                 journal_ref: Optional[dict] = None,
                 realized_pnl: Optional[float] = None,
                 realized_pnl_pct: Optional[float] = None,
                 note: str = "",
                 entry_date: Optional[str] = None) -> dict:
    """组装一条 trade 记录并追加到 trades.jsonl。返回写入的 dict。

    entry_date：当前持仓入场日（开仓 buy 传 None）。用于 _adherence_for 的 D7 cycle 过滤
    （排除上周期 position_action）。adherence 回查失败（best-effort）写 null，不阻断留痕。
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side 必须 buy|sell, got {side}")
    now = datetime.now(_TZ_CN)
    adherence = _adherence_for(
        ts_code=ts_code, side=side, fill_price=fill_price,
        when=now, shares_after=shares_after, entry_date=entry_date,
    )
    record = {
        "trade_id": _next_trade_id(now),
        "ts_code": ts_code,
        "name": name,
        "side": side,
        "fill_price": round(float(fill_price), 4),
        "shares": int(shares),
        "amount": round(float(fill_price) * int(shares), 2),
        "realized_pnl": round(float(realized_pnl), 2) if realized_pnl is not None else None,
        "realized_pnl_pct": round(float(realized_pnl_pct), 4) if realized_pnl_pct is not None else None,
        "avg_cost_after": round(float(avg_cost_after), 4) if avg_cost_after is not None else None,
        "shares_after": int(shares_after) if shares_after is not None else None,
        "strategy": strategy,
        "regime": regime,
        "journal_ref": journal_ref,
        "note": note or "",
        "traded_at": now.isoformat(timespec="seconds"),
        "adherence": adherence,
    }
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return record


def load_trades(ts_code: Optional[str] = None,
                limit: Optional[int] = None,
                since_days: Optional[int] = None) -> list[dict]:
    """读 trades.jsonl,按 traded_at 倒序。可按 ts_code / limit / since_days 过滤。"""
    p = _path()
    if not p.exists():
        return []
    cutoff: Optional[str] = None
    if since_days:
        cutoff = (datetime.now(_TZ_CN) - timedelta(days=since_days)).isoformat()
    out: list[dict] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ts_code and rec.get("ts_code") != ts_code:
                continue
            if cutoff and (rec.get("traded_at") or "") < cutoff:
                continue
            out.append(rec)
    out.sort(key=lambda r: (r.get("traded_at") or "", r.get("trade_id") or ""), reverse=True)
    if limit:
        out = out[:limit]
    return out
