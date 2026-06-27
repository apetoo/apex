"""回测复盘 —— 调 DeepSeek 对一批回测信号做结构化经验总结，反哺 analyze prompt + screener 权重。

入口：
  backtest_review.review(ts_code, lookforward_days) -> dict   # 跑一次 AI，返回结构化复盘

与 postmortem 的区别：
  - postmortem 针对单笔真实平仓（样本少、反馈慢）
  - backtest_review 针对一批模拟回测信号（样本多、廉价虚拟实战），把 aggregate 切片 + 退出归因
    + MAE/MFE 喂给 AI，产出"哪类信号失效 / 止损建议 / 拿不住"等可执行结论

闭环：
  - prompt_injection → 注入 analyze 的 user prompt（"你过去在 X 场景胜率 30%"）
  - strategy_weight_hint → 落盘 backtest_strategy_stats.json → strategy_selector 读取（Task 4）
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from openai import OpenAI

from apex import backtest, config

_TZ_CN = timezone(timedelta(hours=8))


class BacktestReviewError(Exception):
    pass


_TOOL = {
    "type": "function",
    "function": {
        "name": "record_backtest_review",
        "description": "记录回测复盘结论。必须调用此工具，不得省略。",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "≤200 字总体结论：这批信号整体表现 + 最主要的盈亏来源。",
                },
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "enum": ["calibration", "exit", "strategy", "regime", "risk", "entry"],
                                "description": "calibration=置信度校准 / exit=止损止盈出场 / strategy=策略来源 / regime=市场环境 / risk=风控仓位 / entry=入场点",
                            },
                            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                            "description": {"type": "string", "description": "问题描述，引用具体数据（胜率/MAE/MFE/桶）。"},
                            "suggestion": {"type": "string", "description": "可执行改进，1-2 句。"},
                        },
                        "required": ["category", "severity", "description", "suggestion"],
                    },
                    "description": "发现的问题列表，按 severity 降序，3-6 条。",
                },
                "prompt_injection": {
                    "type": "string",
                    "description": "≤150 字，下次 analyze 时注入 user prompt 的提醒。要具体引用失败模式，别套话。无明显问题给空串。",
                },
                "strategy_weight_hint": {
                    "type": "object",
                    "description": "对每个策略来源(source)的权重调整建议。值∈{increase,decrease,hold}。仅对样本≥3 的策略给非 hold。",
                    "additionalProperties": {"type": "string", "enum": ["increase", "decrease", "hold"]},
                },
            },
            "required": ["summary", "findings", "prompt_injection", "strategy_weight_hint"],
        },
    },
}


_SYSTEM_PROMPT = """你是这个 A 股交易系统的回测复盘官。

你将看到一批 AI 看多信号的模拟回测聚合统计（T+1 开盘入场、含佣金印花税、SL/TP intrabar 触发）。
你的任务：从统计里诊断系统性问题，给出可执行改进，并产出一段将注入下次 analyze 的提醒。

诊断维度（按数据说话，避免结果论）：
- calibration：置信度桶胜率是否随置信度单调上升？高置信度反低胜率=校准失真。
- exit：止损组(stop_hit)胜率与平均亏损、止盈组(target_hit)收益厚度、到期组(time_stop)占比。
        left_money_rate（MFE>0 却最终亏损的占比）高=拿不住/止损太紧。
- strategy：各 source 胜率与超额，差源是否该降权。
- regime：若整体跑输 benchmark，是否系统性环境误判。
- risk：MAE 过大=单笔风险敞口失控。

约束：
- findings 3-6 条，按 severity 降序，每条 description 必须引用具体数字。
- prompt_injection 要具体（如"你在 7-10 高置信度桶胜率仅 33%，下次高置信度反而要降低仓位"），不要空话。
- strategy_weight_hint 只对样本≥3 的策略给非 hold，且必须与该策略胜率/超额方向一致。
"""


def _format_stats(agg: dict) -> str:
    """把 aggregate 结果压成给 AI 看的紧凑文本。"""
    lines = [
        f"回测窗口: 持有 {agg['lookforward_days']} 天",
        f"总信号: {agg['total_signals']} · 可成交: {agg['fillable_count']} · 涨停不可成交: {agg['unfillable_count']}",
        "",
        "## 按置信度桶 (calibration) — 期望胜率随置信度上升:",
    ]
    for b in agg.get("by_confidence_bucket", []):
        lines.append(f"  {b['key']}: n={b['n']} 胜率={b['win_rate']} 净收益={b['avg_net_return']} 超额={b['avg_excess_return']}")

    lines.append("\n## 按 verdict:")
    for b in agg.get("by_verdict", []):
        lines.append(f"  {b['key']}: n={b['n']} 胜率={b['win_rate']} 净收益={b['avg_net_return']}")

    lines.append("\n## 按 source/策略 (strategy):")
    for b in agg.get("by_strategy", []):
        lines.append(f"  {b['key']}: n={b['n']} 胜率={b['win_rate']} 净收益={b['avg_net_return']} 超额={b['avg_excess_return']}")

    lines.append("\n## 按退出归因 (exit):")
    for b in agg.get("by_exit_reason", []):
        lines.append(f"  {b['key']}: n={b['n']} 胜率={b['win_rate']} 净收益={b['avg_net_return']}")

    exc = agg.get("excursion") or {}
    lines.append("\n## 偏移 (MAE/MFE):")
    lines.append(f"  平均最大浮亏 MAE={exc.get('avg_mae')} · 平均最大浮盈 MFE={exc.get('avg_mfe')}")
    lines.append(f"  拿不住率(MFE>0 却亏损)={exc.get('left_money_rate')}")

    return "\n".join(lines)


def _stats_path() -> Path:
    cfg = config.get()
    cache_dir = Path((cfg.get("paths") or {}).get("cache_dir", "~/.stock-journal/cache")).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "backtest_strategy_stats.json"


def _review_path() -> Path:
    """落盘最近一次 AI 回测复盘（含 prompt_injection），供 analyze 读取注入。"""
    cfg = config.get()
    cache_dir = Path((cfg.get("paths") or {}).get("cache_dir", "~/.stock-journal/cache")).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "backtest_review.json"


def load_prompt_injection() -> Optional[str]:
    """供 analyze 读取最近一次回测复盘的 prompt_injection。无/空返回 None。

    这是回测复盘→下次分析闭环的最后一环：AI 在回测里发现的失败模式，
    在下次分析同一只票时作为提醒注入 user prompt。
    """
    p = _review_path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    txt = (d.get("prompt_injection") or "").strip()
    return txt or None


def _persist_strategy_stats(agg: dict, weight_hint: dict) -> dict:
    """落盘 per-strategy 模拟胜率 + AI 权重建议，供 strategy_selector 读取（Task 4 闭环）。"""
    payload = {
        "generated_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
        "lookforward_days": agg.get("lookforward_days"),
        "total_signals": agg.get("total_signals"),
        "by_strategy": {
            b["key"]: {
                "n": b["n"],
                "win_rate": b["win_rate"],
                "avg_net_return": b["avg_net_return"],
                "avg_excess_return": b["avg_excess_return"],
                "weight_hint": weight_hint.get(b["key"], "hold"),
            }
            for b in agg.get("by_strategy", [])
        },
    }
    try:
        _stats_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠ backtest_strategy_stats 落盘失败: {e}")
    return payload


def load_strategy_stats() -> Optional[dict]:
    """供 strategy_selector 读取已落盘的回测策略统计。无则 None。"""
    p = _stats_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def review(ts_code: Optional[str] = None,
           lookforward_days: Optional[int] = None,
           model: Optional[str] = None) -> dict:
    """对一批回测信号跑 AI 复盘。返回结构化结论 + 落盘策略统计。

    抛 BacktestReviewError 表示 AI 没产出有效结论。
    """
    cfg = config.get()
    api_key = cfg["deepseek"]["api_key"]
    if not api_key:
        raise BacktestReviewError("deepseek.api_key 未配置")

    agg = backtest.aggregate(ts_code=ts_code, lookforward_days=lookforward_days)
    if agg["fillable_count"] == 0:
        raise BacktestReviewError("无可成交信号，无法复盘")

    user_block = _format_stats(agg)
    client = OpenAI(api_key=api_key, base_url=cfg.get("deepseek", {}).get("base_url", "https://api.deepseek.com"))
    use_model = (
        model
        or cfg.get("postmortem", {}).get("model")
        or cfg.get("screener", {}).get("ai_model")
        or cfg.get("deepseek", {}).get("model", "deepseek-chat")
    )

    # ark 端点偶发返回 finish=tool_calls 但 tool_calls 为空 / JSON 截断，重试兜底。
    last_err = None
    ai_part = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=use_model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_block},
                ],
                tools=[_TOOL],
                tool_choice={"type": "function", "function": {"name": "record_backtest_review"}},
                # deepseek-v4-pro 是推理模型，thinking:disabled 不完全 honor，
                # 实测需 ~2000 token 内部思考后才 emit tool call，故给足 4096。
                max_tokens=4096,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception as e:
            last_err = BacktestReviewError(f"DeepSeek 调用失败: {type(e).__name__}: {e}")
            continue

        msg = resp.choices[0].message
        if not msg.tool_calls:
            last_err = BacktestReviewError(
                f"AI 未调用 record_backtest_review（finish={resp.choices[0].finish_reason}），"
                f"ark 端点偶发空 tool_calls，重试中"
            )
            continue
        try:
            ai_part = json.loads(msg.tool_calls[0].function.arguments)
            break
        except Exception as e:
            last_err = BacktestReviewError(f"AI 返回的 JSON 无效: {e}")
            continue

    if ai_part is None:
        raise last_err or BacktestReviewError("AI 复盘失败")

    weight_hint = ai_part.get("strategy_weight_hint") or {}
    persisted = _persist_strategy_stats(agg, weight_hint)

    result = {
        "summary": ai_part.get("summary", ""),
        "findings": ai_part.get("findings") or [],
        "prompt_injection": ai_part.get("prompt_injection", ""),
        "strategy_weight_hint": weight_hint,
        "strategy_stats": persisted,
        "aggregate": agg,
        "model": use_model,
        "generated_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
    }

    # 落盘整份复盘，供 analyze 下次注入 prompt_injection（闭环最后一环）
    try:
        _review_path().write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠ backtest_review 落盘失败: {e}")

    return result
