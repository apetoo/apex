"""平仓复盘 —— 调 DeepSeek 对单笔 closed_position 做结构化诊断，填回 diagnosis 段。

入口：
  postmortem.run(record) -> dict           # 跑一次 AI，返回 diagnosis（不写盘）
  postmortem.patch_diagnosis(closed_at, diagnosis) -> bool   # 把 diagnosis 写回 jsonl
  postmortem.run_and_patch(record) -> dict | None            # 上面两步组合，UI 一键调用

设计：
  - 不依赖 watchlist（只读 closed_positions.jsonl）；watchlist.close_position 不调它，避免循环依赖
  - 出场后 5 个交易日 K 线作为关键证据：判断"出场过早 / 过晚"
  - 机械字段（outcome_class / calibration_bucket / target_distance_max_pct）由代码算好，AI 只做因果解释
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from openai import OpenAI

from apex import config, data

_TZ_CN = timezone(timedelta(hours=8))


class PostmortemError(Exception):
    pass


_TOOL = {
    "type": "function",
    "function": {
        "name": "record_diagnosis",
        "description": "记录复盘结论。复盘完成后必须调用此工具，不得省略。",
        "parameters": {
            "type": "object",
            "properties": {
                "ai_correctly_identified": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "当时 AI 判断对的点。每条要具体，引用原话或当时记录特征。没有就给空数组。",
                },
                "ai_missed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "当时 AI 没看到 / 误判的点。每条必须能从开仓时已有数据里推出来（避免结果论）。",
                },
                "lesson": {
                    "type": "string",
                    "description": "下次面对类似 setup 时应该具体改变什么行为，1-3 句。",
                },
                "ai_diagnosis_text": {
                    "type": "string",
                    "description": "≤200 字综合复盘文字。",
                },
            },
            "required": ["ai_correctly_identified", "ai_missed", "lesson", "ai_diagnosis_text"],
        },
    },
}


def _load_system_prompt() -> str:
    p = Path(__file__).parent / "prompts" / "postmortem.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return "你是这个交易系统的复盘官。诊断一笔已平仓的交易并调用 record_diagnosis 工具。"


def _bucket_for(confidence: Optional[int]) -> str:
    if confidence is None:
        return "?"
    if confidence <= 3:
        return "1-3"
    if confidence <= 6:
        return "4-6"
    return "7-10"


def _compute_mechanical(record: dict) -> dict:
    """从 record 算出复盘需要的机械字段。"""
    o = record.get("open", {}) or {}
    c = record.get("close", {}) or {}

    pnl = c.get("realized_pnl_pct")
    outcome = "breakeven"
    if pnl is not None:
        if pnl > 0.005:
            outcome = "win"
        elif pnl < -0.005:
            outcome = "loss"

    entry = float(o.get("entry_price") or 0)
    high = c.get("high_during_hold")
    low = c.get("low_during_hold")
    target = o.get("target")
    stop = o.get("stop_loss")

    target_distance_max_pct = None
    if entry > 0 and high is not None:
        target_distance_max_pct = round((float(high) - entry) / entry, 4)

    nearly_hit_target = bool(
        target and high is not None and entry > 0
        and float(high) >= float(target) * 0.95
        and outcome == "loss"
    )

    # 止损是否过紧：持仓期间最低跌破止损（被打掉了），但持仓期间最高又回升超过进场价
    stop_hit_too_tight = bool(
        stop and high is not None and low is not None and entry > 0
        and float(low) <= float(stop)
        and float(high) > entry
    )

    bucket_label = "未知"
    verdict = o.get("ai_verdict")
    confidence = o.get("ai_confidence")
    if verdict and confidence is not None:
        bucket_label = f"{verdict}@{_bucket_for(int(confidence))}"

    return {
        "outcome_class": outcome,
        "target_distance_max_pct": target_distance_max_pct,
        "nearly_hit_target": nearly_hit_target,
        "stop_hit_too_tight": stop_hit_too_tight,
        "calibration_bucket": bucket_label,
    }


def _fetch_post_exit_kline(ts_code: str, exit_date: str, days_forward: int = 7) -> list[dict]:
    """拉出场后 N 个自然日的 K 线（实际可能 < N 个交易日）。失败返回空。"""
    try:
        d = datetime.fromisoformat(exit_date).date()
    except Exception:
        return []
    if d >= datetime.now(_TZ_CN).date():
        return []
    start = (d + timedelta(days=1)).strftime("%Y%m%d")
    end = (d + timedelta(days=days_forward + 5)).strftime("%Y%m%d")
    try:
        raw = data.get_daily_price(ts_code, start_date=start, end_date=end, adj="qfq")
        rows = json.loads(raw)
        if not isinstance(rows, list):
            return []
        return rows[:days_forward]
    except Exception:
        return []


def _format_post_exit(rows: list[dict], entry_price: Optional[float],
                      exit_price: Optional[float]) -> str:
    if not rows:
        return "（出场日为今天或近期，暂无后续数据）"
    lines = ["日期       开盘    收盘    最高    最低    vs 出场价"]
    for r in rows:
        td = str(r.get("trade_date", ""))
        td_fmt = f"{td[:4]}-{td[4:6]}-{td[6:]}" if len(td) == 8 else td
        close = r.get("close", 0)
        diff = ""
        if exit_price and exit_price > 0 and close:
            diff = f"{(close - exit_price) / exit_price * 100:+.2f}%"
        lines.append(
            f"{td_fmt}  {r.get('open',0):>6}  {close:>6}  "
            f"{r.get('high',0):>6}  {r.get('low',0):>6}  {diff:>8}"
        )
    return "\n".join(lines)


def _format_for_ai(record: dict, mech: dict, post_exit: str) -> str:
    o = record.get("open", {}) or {}
    c = record.get("close", {}) or {}

    pnl_pct_str = "-"
    if c.get("realized_pnl_pct") is not None:
        pnl_pct_str = f"{c['realized_pnl_pct'] * 100:.2f}%"

    pnl_amt_str = ""
    if c.get("realized_pnl_amount") is not None:
        pnl_amt_str = f" / {c['realized_pnl_amount']:+,.0f} 元"

    target_dist_str = "?"
    if mech.get("target_distance_max_pct") is not None:
        target_dist_str = f"{mech['target_distance_max_pct'] * 100:+.2f}%"

    features = o.get("ai_features") or {}
    analysis_excerpt = (o.get("ai_analysis_text") or "")[:1500]
    if not analysis_excerpt:
        analysis_excerpt = "（开仓时无 AI 分析正文记录）"

    return f"""## 标的
{record.get('ts_code')} {record.get('name','')}

## 开仓时（{o.get('entry_date')}）
- AI 当时判断：{o.get('ai_verdict','?')} ｜ 置信度 {o.get('ai_confidence','?')}/10
- 开仓价 {o.get('entry_price','?')} ｜ 止损 {o.get('stop_loss','?')} ｜ 目标 {o.get('target','?')}
- 仓位 {o.get('position_size_shares','?')} 股 ｜ 风险敞口 {o.get('risk_amount','?')} 元
- 当时记录的技术特征：{json.dumps(features, ensure_ascii=False)}
- AI 当时分析正文（截前 1500 字）：
{analysis_excerpt}

## 持仓期间
- 持有 {c.get('days_held','?')} 天（{c.get('trading_days_held','?')} 个交易日）
- 期间最高 {c.get('high_during_hold','?')} ｜ 距进场最远 {target_dist_str}
- 期间最低 {c.get('low_during_hold','?')}

## 出场（{c.get('exit_date')}）
- 原因：{c.get('exit_reason')}
- 出场价：{c.get('actual_exit_price')}
- 实际盈亏：{pnl_pct_str}{pnl_amt_str}
- 用户备注：{c.get('user_notes') or '（无）'}

## 出场后续 K 线（关键证据）
{post_exit}

## 机械计算（不要重复给结论，请给因果解释）
- outcome_class: {mech['outcome_class']}
- calibration_bucket: {mech['calibration_bucket']}
- target_distance_max_pct: {target_dist_str}
- nearly_hit_target (≥95%): {mech['nearly_hit_target']}
- stop_hit_too_tight (低点跌破止损但高点回升过进场价): {mech['stop_hit_too_tight']}

请基于以上信息调用 record_diagnosis。"""


def run(record: dict, model: Optional[str] = None) -> dict:
    """对一条 closed_position 跑 AI 复盘。返回 diagnosis dict（机械字段 + AI 字段合并）。

    抛 PostmortemError 表示 AI 没产出有效 diagnosis，调用方应保留 diagnosis=null。
    """
    cfg = config.get()
    api_key = cfg["deepseek"]["api_key"]
    if not api_key:
        raise PostmortemError("deepseek.api_key 未配置")

    mech = _compute_mechanical(record)

    o = record.get("open", {}) or {}
    c = record.get("close", {}) or {}
    post_exit_rows = _fetch_post_exit_kline(record.get("ts_code", ""), c.get("exit_date") or "")
    post_exit_text = _format_post_exit(
        post_exit_rows,
        entry_price=o.get("entry_price"),
        exit_price=c.get("actual_exit_price"),
    )

    user_block = _format_for_ai(record, mech, post_exit_text)
    system = _load_system_prompt()

    client = OpenAI(api_key=api_key, base_url=cfg.get("deepseek", {}).get("base_url", "https://api.deepseek.com"))
    # 优先级：调用方 → postmortem.model → screener.ai_model（与 screener 同模型，已验证支持 tool_choice）
    # 注意：deepseek-reasoner / 推理模型不支持强制 tool_choice，因此不能直接落 deepseek.model
    use_model = (
        model
        or cfg.get("postmortem", {}).get("model")
        or cfg.get("screener", {}).get("ai_model")
        or cfg.get("deepseek", {}).get("model", "deepseek-chat")
    )

    try:
        resp = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_block},
            ],
            tools=[_TOOL],
            tool_choice={"type": "function", "function": {"name": "record_diagnosis"}},
            max_tokens=1024,
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception as e:
        raise PostmortemError(f"DeepSeek 调用失败: {type(e).__name__}: {e}") from e

    try:
        tc = resp.choices[0].message.tool_calls[0]
        ai_part = json.loads(tc.function.arguments)
    except Exception as e:
        raise PostmortemError(f"AI 未按预期调用 record_diagnosis: {e}") from e

    diagnosis = {
        **mech,
        "ai_correctly_identified": ai_part.get("ai_correctly_identified") or [],
        "ai_missed": ai_part.get("ai_missed") or [],
        "lesson": ai_part.get("lesson") or "",
        "ai_diagnosis_text": ai_part.get("ai_diagnosis_text") or "",
        "post_exit_kline_used": len(post_exit_rows),
        "model": use_model,
        "diagnosed_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
    }
    return diagnosis


def _closed_positions_path() -> Path:
    cfg = config.get()
    journal_dir = Path(cfg["paths"]["journal_dir"]).expanduser()
    return journal_dir / "closed_positions.jsonl"


def patch_diagnosis(closed_at: str, diagnosis: dict) -> bool:
    """把 jsonl 里 close.closed_at == closed_at 的那一行的 diagnosis 字段更新。

    使用 .tmp + rename 原子替换，避免半写状态。返回是否找到并 patch。
    """
    if not closed_at:
        return False
    path = _closed_positions_path()
    if not path.exists():
        return False

    lines: list[str] = []
    matched = False
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                lines.append(raw)
                continue
            if (rec.get("close") or {}).get("closed_at") == closed_at:
                rec["diagnosis"] = diagnosis
                lines.append(json.dumps(rec, ensure_ascii=False, default=str))
                matched = True
            else:
                lines.append(raw)

    if not matched:
        return False

    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            for line in lines:
                out.write(line + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    return True


def run_and_patch(record: dict) -> Optional[dict]:
    """跑复盘并把 diagnosis 写回 jsonl。失败返回 None（不抛），让 UI 决定是否提示用户。"""
    closed_at = (record.get("close") or {}).get("closed_at")
    if not closed_at:
        return None
    try:
        diagnosis = run(record)
    except PostmortemError as e:
        print(f"[postmortem] {record.get('ts_code')} 复盘失败: {e}")
        return None
    try:
        patch_diagnosis(closed_at, diagnosis)
    except Exception as e:
        print(f"[postmortem] 写回 jsonl 失败: {e}")
        return None
    return diagnosis
