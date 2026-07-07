"""持仓推送服务：apex 主动调用消费方 webhook，把持仓变更推出去。

接口文档：docs/integration/positions-push-api.md
不设应用层鉴权（明文 JSON）。

两种推送：
  - 增量 notify()：持仓状态一变即推，fire-and-forget（后台线程），不阻断主流程。
  - 全量 push_full()：当前 active_positions 快照，同步发送并返回结果（供 /api/push/full 调用）。

投递语义：at-least-once + 指数退避重试 + push_id 幂等 + seq 单调递增（缺口检测）。
失败旁路写 push_log.jsonl，不抛异常给调用方。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import config

_log = logging.getLogger("apex.push")
_TZ_CN = timezone(timedelta(hours=8))
_SOURCE_VERSION = "1.0.0"

_seq_lock = threading.Lock()
_log_lock = threading.Lock()


# ── 配置读取 ────────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    return config.get().get("push", {}) or {}


def _enabled() -> bool:
    return bool(_cfg().get("enabled"))


def _incremental_enabled() -> bool:
    return _enabled() and bool(_cfg().get("incremental", {}).get("enabled", True))


def _journal_dir() -> Path:
    return Path(config.get()["paths"]["journal_dir"]).expanduser()


def _seq_path() -> Path:
    return _journal_dir() / "push_seq.json"


def _log_path() -> Path:
    p = _cfg().get("log_file") or "~/.stock-journal/push_log.jsonl"
    return Path(p).expanduser()


def _payloads_dir() -> Path:
    return _log_path().parent / "push_payloads"


# ── 工具 ────────────────────────────────────────────────────────────────────────

def _ulid() -> str:
    """无外部依赖的唯一 ID（uuid4 大写 hex，长度 32）。非严格 ULID，但全局唯一够用。"""
    return uuid.uuid4().hex.upper()


def _now_iso() -> str:
    return datetime.now(_TZ_CN).isoformat(timespec="seconds")


def _next_seq() -> int:
    """线程安全的单调递增序列号，持久化到 push_seq.json。"""
    with _seq_lock:
        path = _seq_path()
        cur = 0
        if path.exists():
            try:
                cur = int(json.loads(path.read_text(encoding="utf-8")).get("seq", 0))
            except Exception:  # noqa: BLE001
                cur = 0
        nxt = cur + 1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"seq": nxt}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return nxt


def _f(v, ndigits=None):
    """浮点规整：None 透传，否则 round。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return round(f, ndigits) if ndigits is not None else f


# ── 字段映射：内部存储 → 对外 schema ────────────────────────────────────────────

def _to_position(pos: Optional[dict]) -> Optional[dict]:
    """active_positions 内部 record → 外部 Position schema（见文档 §7.1）。"""
    if not pos:
        return None
    shares = int(pos.get("position_size_shares") or 0)
    return {
        "ts_code": pos.get("ts_code"),
        "name": pos.get("name", "") or "",
        "entry_date": pos.get("entry_date"),
        "entry_price": _f(pos.get("entry_price")),
        "avg_cost": _f(pos.get("avg_cost") or pos.get("entry_price")),
        "shares": shares,
        "lots": shares // 100,
        "stop_loss": _f(pos.get("stop_loss")),
        "target": _f(pos.get("target")),
        "risk_amount": _f(pos.get("risk_amount")),
        "calibrated_confidence": _f(pos.get("calibrated_confidence")),
        "strategy": pos.get("strategy"),
        "regime_at_open": pos.get("regime_at_open"),
        "expires_at": pos.get("expires_at"),
        "status": pos.get("status", "active"),
    }


def _to_trade(trade: Optional[dict], exit_reason: Optional[str] = None) -> Optional[dict]:
    """trades.jsonl 内部 record → 外部 Trade schema（见文档 §7.2）。

    exit_reason: trades.jsonl 不存此字段；sell 事件由调用方传入覆盖。
    """
    if not trade:
        return None
    out = {
        "trade_id": trade.get("trade_id"),
        "side": trade.get("side"),
        "fill_price": _f(trade.get("fill_price")),
        "shares": trade.get("shares"),
        "amount": _f(trade.get("amount")),
        "realized_pnl": _f(trade.get("realized_pnl")),
        "realized_pnl_pct": _f(trade.get("realized_pnl_pct")),
        "avg_cost_after": _f(trade.get("avg_cost_after")),
        "shares_after": trade.get("shares_after"),
        "strategy": trade.get("strategy"),
        "regime": trade.get("regime"),
        "exit_reason": trade.get("exit_reason") or exit_reason,
        "note": trade.get("note", "") or "",
        "traded_at": trade.get("traded_at"),
    }
    return out


def _to_close(close: Optional[dict]) -> Optional[dict]:
    """closed_positions.jsonl 的 close 段 → 外部 Close schema（见文档 §7.4）。"""
    if not close:
        return None
    return {
        "exit_date": close.get("exit_date"),
        "actual_exit_price": _f(close.get("actual_exit_price")),
        "exit_reason": close.get("exit_reason"),
        "days_held": close.get("days_held"),
        "trading_days_held": close.get("trading_days_held"),
        "high_during_hold": _f(close.get("high_during_hold")),
        "low_during_hold": _f(close.get("low_during_hold")),
        "realized_pnl_pct": _f(close.get("realized_pnl_pct")),
        "realized_pnl_amount": _f(close.get("realized_pnl_amount")),
        "closed_at": close.get("closed_at"),
    }


# ── 日志 / payload 持久化 ───────────────────────────────────────────────────────

def _store_payload(envelope: dict) -> None:
    """把 envelope 落到 push_payloads/<push_id>.json，供 replay 重发。失败静默。"""
    try:
        d = _payloads_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{envelope['push_id']}.json").write_text(
            json.dumps(envelope, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def _write_log(envelope: dict, attempt: int, status: str,
               http_status: Optional[int], error: Optional[str], t0: float) -> None:
    """每次尝试（含重试）写一行审计日志到 push_log.jsonl。"""
    data = envelope.get("data") or {}
    entry = {
        "push_id": envelope.get("push_id"),
        "seq": envelope.get("seq"),
        "push_type": envelope.get("push_type"),
        "event_type": data.get("event_type"),
        "ts_code": data.get("ts_code"),
        "attempt": attempt,
        "status": status,
        "http_status": http_status,
        "error": error,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "sent_at": _now_iso(),
    }
    with _log_lock:
        path = _log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001
            pass


# ── 发送（含重试） ──────────────────────────────────────────────────────────────

def _send(envelope: dict) -> dict:
    """发送一个 envelope，含指数退避重试。返回结果 dict。"""
    cfg = _cfg()
    base = (cfg.get("base_url") or "").rstrip("/")
    path = cfg.get("path") or "/positions/push"
    url = base + path
    if not base:
        res = {"status": "failed", "http_status": None, "attempts": 0,
               "error": "push.base_url 未配置", "latency_ms": 0}
        _write_log(envelope, 0, "failed", None, "base_url not configured", time.monotonic())
        return res

    timeout = float(cfg.get("timeout_seconds", 10))
    retry_cfg = cfg.get("retry") or {}
    max_attempts = int(retry_cfg.get("max_attempts", 5))
    base_s = float(retry_cfg.get("base_seconds", 1))
    max_s = float(retry_cfg.get("max_seconds", 60))

    body = json.dumps(envelope, ensure_ascii=False, default=str).encode("utf-8")
    t0 = time.monotonic()
    last_err: Optional[str] = None
    http_status: Optional[int] = None

    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                http_status = resp.getcode()
                if 200 <= http_status < 300:
                    _write_log(envelope, attempt, "success", http_status, None, t0)
                    return {"status": "success", "http_status": http_status,
                            "attempts": attempt, "latency_ms": int((time.monotonic() - t0) * 1000)}
                # 非 2xx：4xx（除 408/429）永久失败
                if 400 <= http_status < 500 and http_status not in (408, 429):
                    last_err = f"http {http_status}"
                    _write_log(envelope, attempt, "failed", http_status, last_err, t0)
                    return {"status": "failed", "http_status": http_status,
                            "attempts": attempt, "error": last_err,
                            "latency_ms": int((time.monotonic() - t0) * 1000)}
                last_err = f"http {http_status}"
        except urllib.error.HTTPError as e:
            http_status = e.code
            if 400 <= e.code < 500 and e.code not in (408, 429):
                last_err = f"http {e.code}: {e.reason}"
                _write_log(envelope, attempt, "failed", e.code, last_err, t0)
                return {"status": "failed", "http_status": e.code,
                        "attempts": attempt, "error": last_err,
                        "latency_ms": int((time.monotonic() - t0) * 1000)}
            last_err = f"http {e.code}: {e.reason}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            http_status = None
            last_err = str(e)
        except Exception as e:  # noqa: BLE001
            http_status = None
            last_err = str(e)

        if attempt < max_attempts:
            time.sleep(min(max_s, base_s * (2 ** (attempt - 1))))

    _write_log(envelope, max_attempts, "failed", http_status, last_err, t0)
    return {"status": "failed", "http_status": http_status,
            "attempts": max_attempts, "error": last_err,
            "latency_ms": int((time.monotonic() - t0) * 1000)}


# ── 增量推送 ────────────────────────────────────────────────────────────────────

def notify(event_type: str, *,
           ts_code: str,
           name: str = "",
           before: Optional[dict] = None,
           after: Optional[dict] = None,
           trade: Optional[dict] = None,
           close: Optional[dict] = None,
           exit_reason: Optional[str] = None) -> None:
    """持仓变更增量推送。fire-and-forget（后台线程），失败不阻断调用方。

    event_type 见文档 §6.2。before/after 为内部 active_positions record（notify 内部映射）。
    trade 为内部 trades.jsonl record。close 为 closed_record['close'] 段。
    """
    if not _incremental_enabled():
        return
    try:
        trade_out = _to_trade(trade, exit_reason=exit_reason)
        data = {
            "event_type": event_type,
            "event_id": (trade or {}).get("trade_id") or _ulid(),
            "event_at": _now_iso(),
            "ts_code": ts_code,
            "name": name or "",
            "before": _to_position(before),
            "after": _to_position(after),
            "trade": trade_out,
            "close": _to_close(close),
        }
        envelope = {
            "push_type": "incremental",
            "push_id": _ulid(),
            "pushed_at": _now_iso(),
            "source": "apex",
            "source_version": _SOURCE_VERSION,
            "seq": _next_seq(),
            "data": data,
        }
        _store_payload(envelope)
        threading.Thread(target=_send, args=(envelope,), daemon=True).start()
    except Exception:  # noqa: BLE001 — 推送是旁路，任何异常都不阻断主流程
        _log.debug("push.notify 失败（已吞）", exc_info=True)


# ── 全量推送 ────────────────────────────────────────────────────────────────────

def push_full() -> dict:
    """全量快照推送，同步发送。返回 _send 结果 dict（供 /api/push/full）。"""
    if not _enabled():
        return {"status": "disabled"}
    try:
        from apex import account as _acc
        from apex import data as _data
        from apex import watchlist as _wl

        wl = _wl.load()
        positions = wl.get("active_positions", []) or []
        pos_out = [_to_position(p) for p in positions]

        # 实时市值（best-effort，拉不到行情则三个合计字段为 null）
        codes = [p.get("ts_code") for p in positions if p.get("ts_code")]
        prices: dict = {}
        if codes:
            try:
                prices = _data.get_realtime_price(codes) or {}
                if not any(v for v in prices.values()):
                    prices = _data.get_latest_price(codes) or {}
            except Exception:  # noqa: BLE001
                prices = {}

        total_mv = 0.0
        total_cost = 0.0
        for p in positions:
            sh = int(p.get("position_size_shares") or 0)
            cost = float(p.get("avg_cost") or p.get("entry_price") or 0)
            total_cost += sh * cost
            px = prices.get(p.get("ts_code"))
            if px:
                total_mv += sh * float(px)
        has_px = any(prices.values())

        account_out = None
        try:
            a = _acc.load()
            account_out = {
                "total_capital": _f(a.get("total_capital")),
                "risk_per_trade_pct": a.get("risk_per_trade_pct"),
                "max_total_risk_pct": a.get("max_total_risk_pct"),
                "currency": "CNY",
                "updated_at": a.get("updated_at"),
            }
        except Exception:  # noqa: BLE001
            account_out = None

        data = {
            "snapshot_at": _now_iso(),
            "account": account_out,
            "positions": pos_out,
            "position_count": len(pos_out),
            "total_market_value": round(total_mv, 2) if has_px else None,
            "total_cost": round(total_cost, 2),
            "total_unrealized_pnl": round(total_mv - total_cost, 2) if has_px else None,
        }
        envelope = {
            "push_type": "full",
            "push_id": _ulid(),
            "pushed_at": _now_iso(),
            "source": "apex",
            "source_version": _SOURCE_VERSION,
            "seq": _next_seq(),
            "data": data,
        }
        _store_payload(envelope)
        return _send(envelope)
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "attempts": 0, "error": str(e), "latency_ms": 0}


# ── 管理端点支撑 ────────────────────────────────────────────────────────────────

def status() -> dict:
    """返回推送当前状态。"""
    last_seq = 0
    seq_path = _seq_path()
    if seq_path.exists():
        try:
            last_seq = int(json.loads(seq_path.read_text(encoding="utf-8")).get("seq", 0))
        except Exception:  # noqa: BLE001
            pass

    last: Optional[dict] = None
    log_path = _log_path()
    if log_path.exists():
        try:
            lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                last = json.loads(lines[-1])
        except Exception:  # noqa: BLE001
            pass

    return {
        "enabled": _enabled(),
        "incremental_enabled": _incremental_enabled(),
        "base_url": _cfg().get("base_url") or "",
        "last_seq": last_seq,
        "last_push_at": (last or {}).get("sent_at"),
        "last_status": (last or {}).get("status"),
        "last_http_status": (last or {}).get("http_status"),
    }


def load_log(limit: int = 100) -> list[dict]:
    """读最近 N 条推送日志（按 seq 倒序），去掉大 payload 字段保持响应小。"""
    path = _log_path()
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append(rec)
    out.sort(key=lambda r: (r.get("seq") or 0, r.get("attempt") or 0), reverse=True)
    return out[:limit]


def replay(since: int = 0) -> dict:
    """重放 seq > since 的推送（从 push_payloads/ 读 envelope 重发）。

    保留原 push_id —— 消费方按 push_id 去重：已收的会被跳过，漏的才补。
    """
    if not _enabled():
        return {"status": "disabled", "replayed": 0}
    d = _payloads_dir()
    if not d.exists():
        return {"replayed": 0, "results": []}
    to_replay: list[dict] = []
    for f in d.glob("*.json"):
        try:
            env = json.loads(f.read_text(encoding="utf-8"))
            if int(env.get("seq") or 0) > since:
                to_replay.append(env)
        except Exception:  # noqa: BLE001
            continue
    to_replay.sort(key=lambda e: int(e.get("seq") or 0))
    results = []
    for env in to_replay:
        r = _send(env)
        results.append({"push_id": env.get("push_id"), "seq": env.get("seq"),
                        "push_type": env.get("push_type"), "result": r})
    return {"replayed": len(results), "results": results}
