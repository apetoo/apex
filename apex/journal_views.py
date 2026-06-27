"""Journal entry 派生视图 —— 同一条 entry, 按消费场景压成不同精度。

存储层(``<ts_code>.jsonl``) 保持 append-only 原始全量(判决 + features + evidence[]
+ analysis_text + market_context + ...); 这里只做"读出来后压成什么"的派生,
**不写入、不落盘**。三个消费场景要的精度不同:

  - ``history_digest``: analyze 注入历史用(判决 + 1 条关键证据 + 后续表现), ~80 tokens/条
  - ``chat_summary``:   chat 工具返回概要(判决 + 建议价 + top2 证据), ~250 tokens
  - ``chat_full``:      chat 工具返回全量(summary + 完整证据 + features + 叙述), 原始大小

下游消费方(backtest / calibration / journal 页) 直接读 jsonl 拿全量, 不走这里。
"""
from __future__ import annotations

from typing import Optional


def _top_evidence(entry: dict, n: int = 1, max_chars: int = 60) -> list[str]:
    """取前 n 条 evidence, 每条截断到 max_chars。缺失返回 []。"""
    ev = entry.get("evidence") or []
    out: list[str] = []
    for e in ev[:n]:
        s = str(e)
        if len(s) > max_chars:
            s = s[:max_chars] + "…"
        out.append(s)
    return out


def _conf_str(entry: dict) -> str:
    """置信度 + 校准置信度。如 '4/10 校准3'。"""
    conf = entry.get("confidence", "?")
    cal = entry.get("calibrated_confidence")
    cal_str = f" 校准{cal:.0f}" if isinstance(cal, (int, float)) else ""
    return f"{conf}/10{cal_str}"


def _when(entry: dict) -> str:
    """分析时间, 截到分钟。analyzed_at 优先, 回退 date。"""
    raw = entry.get("analyzed_at") or entry.get("date") or "?"
    return str(raw)[:16].replace("T", " ")


def history_digest(entry: dict, outcome: str = "") -> str:
    """一行式给 analyze 注入历史用。

    格式: ``- 时间 | 判决 (置信度/10 校准N) | 「关键证据」 → 后续表现``

    ``outcome`` 是 ``analyze._format_history`` 算出的"→ 实际 +X%"后续表现串,
    透传拼接(可空)。每条 ~80 tokens。
    """
    verdict = entry.get("verdict", "?")
    ev = _top_evidence(entry, 1, 60)
    ev_str = f" | 「{ev[0]}」" if ev else ""
    return f"- {_when(entry)} | {verdict} ({_conf_str(entry)}){ev_str}{outcome}"


def chat_summary(entry: dict, is_today: bool = False) -> str:
    """chat 工具返回的概要。

    含: 当日/历史标记 + 标的 + 时间 + 判决(置信度+校准) + price_advice + top2 证据。
    ~250 tokens。够模型引用"上次说看空因为X"。
    """
    ts = entry.get("ts_code", "?")
    verdict = entry.get("verdict", "?")
    today_tag = "【当日】" if is_today else "【历史】"
    lines = [f"{today_tag} {ts} {_when(entry)} | {verdict} ({_conf_str(entry)})"]

    pa = entry.get("price_advice") or {}
    if isinstance(pa, dict):
        entry_p = pa.get("entry")
        stop = pa.get("stop_loss")
        target = pa.get("target")
        if any(x is not None for x in (entry_p, stop, target)):
            lines.append(
                f"  建议入场 {entry_p if entry_p is not None else '-'} / "
                f"止损 {stop if stop is not None else '-'} / "
                f"目标 {target if target is not None else '-'}"
            )

    for ev in _top_evidence(entry, 2, 60):
        lines.append(f"  - {ev}")
    return "\n".join(lines)


def chat_full(entry: dict, is_today: bool = False) -> str:
    """chat 工具返回的全量。

    chat_summary + 完整证据(超过 top2 的部分) + features + analysis_text 叙述。
    原始大小。trace 链(events)不在此列——单条 200KB+ 会爆 context, 需要时另开工具。
    """
    parts = [chat_summary(entry, is_today)]

    ev_all = entry.get("evidence") or []
    if len(ev_all) > 2:
        parts.append("  完整证据:")
        for e in ev_all:
            parts.append(f"  - {e}")

    feats = entry.get("features") or {}
    if isinstance(feats, dict) and feats:
        kv = " / ".join(f"{k}={v}" for k, v in feats.items() if v is not None)
        if kv:
            parts.append(f"  特征: {kv}")

    text = entry.get("analysis_text") or ""
    if text:
        parts.append("  叙述:")
        parts.append(text)
    return "\n".join(parts)


def is_today_entry(entry: Optional[dict]) -> bool:
    """判断 entry 是否"当日"分析: analyzed_at 落在今日交易时段(9:25-15:05)内。

    交易时段边界: 9:25 集合竞价开始 ~ 15:05 收盘后尾巴。跨出即"历史仅供参考"。
    解析失败 / 无值 → False(历史)。
    """
    if not entry:
        return False
    from datetime import datetime, time as dtime
    raw = entry.get("analyzed_at") or entry.get("date")
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return False
    now = datetime.now()
    if dt.date() != now.date():
        return False
    t = dt.time()
    return dtime(9, 25) <= t <= dtime(15, 5)
