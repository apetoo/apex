"""盘中触发监控 —— 拉实时价 → 检查 candidate trigger / 持仓 stop_loss/target → 写日志 + 推送。

CLI:
  python -m apex.monitor          # 跑一次（任何时间）
  python -m apex.monitor --loop   # 仅交易时段每 60s 跑一次

去重：同 (ts_code, trigger_type, trigger_price) 24h 内只推一次，避免反复抖动。

Streamlit 也可以直接调 check_once() 在用户刷新时跑一遍。
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import config, data, notify, watchlist

_TZ_CN = timezone(timedelta(hours=8))


def _triggers_path() -> Path:
    cfg = config.get()
    journal_dir = Path(cfg["paths"]["journal_dir"]).expanduser()
    return journal_dir / "triggers.jsonl"


def _is_trading_hours() -> bool:
    now = datetime.now(_TZ_CN)
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    return ("09:25" <= hm <= "11:30") or ("12:55" <= hm <= "15:05")


def _signature(rec: dict) -> str:
    return f"{rec.get('ts_code')}|{rec.get('trigger_type')}|{rec.get('trigger_price')}"


def _scan_jsonl(path: Path) -> tuple[list[dict], set[str]]:
    """单次扫描 triggers.jsonl，返回 (trigger 记录列表, acked 签名集合)。
    ack 记录格式：{kind: 'ack', signature: '<ts>|<type>|<price>', acked_at: '...'}。"""
    triggers: list[dict] = []
    acked: set[str] = set()
    if not path.exists():
        return triggers, acked
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") == "ack":
                if rec.get("signature"):
                    acked.add(rec["signature"])
            else:
                triggers.append(rec)
    return triggers, acked


def _load_recent_signatures(path: Path, hours: int = 24) -> set[str]:
    """近 N 小时已检测的 (ts_code|trigger_type|trigger_price) 签名集合，去重用。"""
    triggers, _ = _scan_jsonl(path)
    if not triggers:
        return set()
    cutoff = (datetime.now(_TZ_CN) - timedelta(hours=hours)).isoformat()
    return {_signature(r) for r in triggers if (r.get("detected_at") or "") >= cutoff}


def ack_trigger(signature: str) -> None:
    """标记一条触发为已读，追加 ack 记录到 jsonl。幂等。"""
    if not signature:
        return
    path = _triggers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "kind": "ack",
        "signature": signature,
        "acked_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _classify_position_trigger(pos: dict, price: float) -> Optional[dict]:
    stop = pos.get("stop_loss")
    target = pos.get("target")
    if stop and price <= float(stop):
        return {"type": "stop_loss", "trigger_price": float(stop)}
    if target and price >= float(target):
        return {"type": "target", "trigger_price": float(target)}
    return None


def _classify_candidate_trigger(cand: dict, price: float) -> Optional[dict]:
    if not watchlist.is_triggered(cand, price):
        return None
    tp = cand.get("trigger_price")
    if tp is None:
        return None
    return {"type": "candidate", "trigger_price": float(tp)}


def _emit_trigger(rec: dict, ts_code: str, name: str, section: str, price: float, cls: dict) -> dict:
    out = {
        "ts_code": ts_code,
        "name": name,
        "trigger_type": cls["type"],
        "trigger_price": cls["trigger_price"],
        "current_price": float(price),
        "section": section,
        "detected_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
    }
    # 带上建议价上下文：推送/前端直接看到止损/目标，不用再翻 watchlist 找候选 record
    if section == "candidates":
        for k in ("stop_advice", "target_advice", "trigger_low", "trigger_high", "note"):
            v = rec.get(k)
            if v is not None:
                out[k] = v
    else:  # active_positions
        for k in ("stop_loss", "target", "entry_price"):
            v = rec.get(k)
            if v is not None:
                out[k] = v
    return out


def check_once(notify_enabled: bool = True,
               prices: Optional[dict] = None) -> dict:
    """跑一次触发检查。返回 {triggers: [...], notified: N}。

    prices: 可选预先获取的实时价 dict，避免 streamlit 内重复调 get_realtime_price。
    """
    wl = watchlist.load()
    codes = [
        x["ts_code"]
        for section in ("active_positions", "candidates")
        for x in wl.get(section, [])
        if x.get("ts_code")
    ]
    if not codes:
        return {"triggers": [], "notified": 0}

    if prices is None:
        prices = data.get_realtime_price(codes)

    path = _triggers_path()
    seen = _load_recent_signatures(path)
    new_triggers: list[dict] = []

    for pos in wl.get("active_positions", []):
        code = pos.get("ts_code")
        price = prices.get(code)
        if not price:
            continue
        cls = _classify_position_trigger(pos, price)
        if not cls:
            continue
        sig = f"{code}|{cls['type']}|{cls['trigger_price']}"
        if sig in seen:
            continue
        new_triggers.append(_emit_trigger(pos, code, pos.get("name", ""),
                                          "active_positions", price, cls))
        seen.add(sig)

    for cand in wl.get("candidates", []):
        code = cand.get("ts_code")
        price = prices.get(code)
        if not price:
            continue
        cls = _classify_candidate_trigger(cand, price)
        if not cls:
            continue
        sig = f"{code}|{cls['type']}|{cls['trigger_price']}"
        if sig in seen:
            continue
        new_triggers.append(_emit_trigger(cand, code, cand.get("name", ""),
                                          "candidates", price, cls))
        seen.add(sig)

    if new_triggers:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for t in new_triggers:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")

    notified = 0
    if notify_enabled:
        for t in new_triggers:
            label = {
                "stop_loss": "🛑 止损触发",
                "target": "🎯 目标触发",
                "candidate": "⚡️ 候选触发",
            }.get(t["trigger_type"], "🔔 触发")
            title = f"{label} {t['name']} {t['ts_code']}"
            parts = [f"现价 {t['current_price']}", f"触发位 {t['trigger_price']}"]
            stop = t.get("stop_advice") or t.get("stop_loss")
            tgt = t.get("target_advice") or t.get("target")
            if stop is not None:
                parts.append(f"止损 {stop}")
            if tgt is not None:
                parts.append(f"目标 {tgt}")
            body = " ｜ ".join(parts)
            try:
                notify.notify(title, body)
                notified += 1
            except Exception as e:
                print(f"[monitor] notify failed for {t['ts_code']}: {e}")

    return {"triggers": new_triggers, "notified": notified}


def load_recent_triggers(hours: int = 24, include_acked: bool = False) -> list[dict]:
    """近 N 小时的触发记录（UI 用，最新在前）。默认过滤已读。"""
    path = _triggers_path()
    triggers, acked = _scan_jsonl(path)
    if not triggers:
        return []
    cutoff = (datetime.now(_TZ_CN) - timedelta(hours=hours)).isoformat()
    out: list[dict] = []
    for r in triggers:
        if (r.get("detected_at") or "") < cutoff:
            continue
        if not include_acked and _signature(r) in acked:
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("detected_at") or "", reverse=True)
    return out


def loop(interval_seconds: int = 60) -> None:
    """长连模式：仅交易时间段每 interval 秒跑一次。Ctrl+C 停止。"""
    print(f"[monitor] started; interval={interval_seconds}s; only running during trading hours")
    while True:
        try:
            if _is_trading_hours():
                res = check_once()
                if res["triggers"]:
                    print(
                        f"[monitor] {datetime.now(_TZ_CN).strftime('%H:%M:%S')} "
                        f"→ {len(res['triggers'])} new triggers, notified={res['notified']}"
                    )
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("\n[monitor] stopped")
            break
        except Exception as e:
            print(f"[monitor] error: {type(e).__name__}: {e}; sleeping...")
            time.sleep(interval_seconds)


if __name__ == "__main__":
    import sys

    if "--loop" in sys.argv:
        loop()
    else:
        res = check_once()
        if res["triggers"]:
            print(f"✓ {len(res['triggers'])} 个新触发，已推送 {res['notified']} 条：")
            for t in res["triggers"]:
                print(f"  - {t['ts_code']} {t['name']} | {t['trigger_type']} @ "
                      f"现价 {t['current_price']} (触发位 {t['trigger_price']})")
        else:
            print("无新触发")
