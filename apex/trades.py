"""交易流水审计:每笔买入/卖出追加一行到 ~/.stock-journal/trades.jsonl。

留痕供未来 AI 操作诊断(买卖时机/加仓减仓节奏)。本轮只写不分析。
字段形状见 docs/superpowers/specs/2026-06-27-manual-positions-trades-design.md。
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import config

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
                 note: str = "") -> dict:
    """组装一条 trade 记录并追加到 trades.jsonl。返回写入的 dict。"""
    if side not in ("buy", "sell"):
        raise ValueError(f"side 必须 buy|sell, got {side}")
    now = datetime.now(_TZ_CN)
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