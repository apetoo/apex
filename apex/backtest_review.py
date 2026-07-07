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
from apex.llm import make_client

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

你将看到一批 AI 看多信号的模拟回测聚合统计（T+1 开盘入场、含佣金印花税滑点、SL/TP intrabar 触发、跌停封板顺延）。
统计来自 TRAIN 期；最近一段 OOS 期由系统独立验证你的 weight_hint，样本内看着好但 OOS 亏的策略会被降级 hold。
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


def _review_cfg() -> dict:
    """读 backtest 复盘相关阈值。缺失回退默认。"""
    cfg = config.get()
    bt = cfg.get("backtest") or {}
    return {
        "min_review_samples": int(bt.get("min_review_samples", 8)),
        "max_inject_age_days": int(bt.get("max_inject_age_days", 30)),
        "oos_test_days": int(bt.get("oos_test_days", 30)),
    }


def _oos_split(entries: list, oos_test_days: int) -> tuple[list, list, Optional[str]]:
    """按日期切 train/test：最近 oos_test_days 天信号作 test，更早的作 train。

    返回 (train, test, cutoff_date)。用 cutoff = max_date - oos_test_days；
    信号 date 字符串字典序即日期序。test 为空（数据跨度太短）→ train=全部, test=[]。
    """
    dated = [e for e in entries if e.get("date")]
    if not dated:
        return list(entries), [], None
    dates = sorted(e["date"] for e in dated)
    max_date = dates[-1]
    # 字符串日期减天数：转 datetime 算
    try:
        from datetime import datetime as _dt, timedelta as _td
        cutoff_dt = _dt.strptime(max_date, "%Y-%m-%d") - _td(days=oos_test_days)
        cutoff = cutoff_dt.strftime("%Y-%m-%d")
    except Exception:
        return list(entries), [], None
    train = [e for e in entries if (e.get("date") or "") < cutoff]
    test = [e for e in entries if (e.get("date") or "") >= cutoff]
    # 防极端：train 空（几乎全在 test 窗）→ 退化为全量 train, 不做 OOS
    if not train:
        return list(entries), [], None
    return train, test, cutoff


def _gate_hints_with_oos(weight_hint: dict, agg_test: Optional[dict],
                         min_samples: int) -> tuple[dict, list]:
    """OOS 门控：用 test 集否决被样本内噪声误导的 weight_hint。

    - hint=increase 但 test 该策略 avg_net_return<0 → 降级 hold（样本内看着好，样本外亏）
    - hint=decrease 但 test avg_net_return>0 → 降级 hold（样本内看着差，样本外反而赚）
    - test 缺该策略 / test 样本不足 → 不门控（保留 hint，标注 oos_insufficient）

    返回 (gated_hint, per_strategy 验证明细)。
    """
    per_strategy: list = []
    if not agg_test:
        return dict(weight_hint), per_strategy

    test_fillable = agg_test.get("fillable_count", 0)
    test_by_strat = {b["key"]: b for b in agg_test.get("by_strategy", [])}
    oos_insufficient = test_fillable < min_samples

    gated = {}
    for strat, hint in (weight_hint or {}).items():
        tb = test_by_strat.get(strat)
        test_net = tb.get("avg_net_return") if tb else None
        survived = True
        reason = "ok"
        if not oos_insufficient and test_net is not None and hint in ("increase", "decrease"):
            if hint == "increase" and test_net < 0:
                gated[strat] = "hold"
                survived = False
                reason = "oos_negative_contradicts_increase"
            elif hint == "decrease" and test_net > 0:
                gated[strat] = "hold"
                survived = False
                reason = "oos_positive_contradicts_decrease"
            else:
                gated[strat] = hint
        else:
            gated[strat] = hint
            reason = "oos_insufficient" if oos_insufficient else "no_test_data"
        per_strategy.append({
            "strategy": strat,
            "hint_train": hint,
            "hint_gated": gated[strat],
            "test_n": tb.get("n") if tb else 0,
            "test_win_rate": tb.get("win_rate") if tb else None,
            "test_avg_net_return": test_net,
            "survived_oos": survived,
            "reason": reason,
        })
    return gated, per_strategy


def load_prompt_injection() -> Optional[str]:
    """供 analyze 读取最近一次回测复盘的 prompt_injection。无/空返回 None。

    这是回测复盘→下次分析闭环的最后一环：AI 在回测里发现的失败模式，
    在下次分析时作为提醒注入 user prompt。

    质量保护（避免旧/小样本结论误导新分析）：
    - 样本不足（fillable_count < min_review_samples）→ 不注入
    - 过期（generated_at 超过 max_inject_age_days 天）→ 不注入
    """
    p = _review_path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    rc = _review_cfg()
    # 样本量门槛
    agg = d.get("aggregate") or {}
    fillable = agg.get("fillable_count")
    if fillable is not None and fillable < rc["min_review_samples"]:
        return None
    # 时效门槛
    gen = d.get("generated_at")
    if gen:
        try:
            gen_dt = datetime.fromisoformat(gen)
            age_days = (datetime.now(_TZ_CN) - gen_dt).days
            if age_days > rc["max_inject_age_days"]:
                return None
        except Exception:
            pass
    txt = (d.get("prompt_injection") or "").strip()
    return txt or None


def _sanitize_weight_hint(agg: dict, raw_hint: dict) -> dict:
    """强制样本门槛：n < 3 的策略 weight_hint 一律 'hold'，不采信 AI 对小样本的判断。

    prompt 已要求 AI 自律，但代码兜底——AI 仍可能对 n=1 的策略给 increase/decrease。
    """
    n_by_strategy = {b["key"]: b.get("n", 0) for b in agg.get("by_strategy", [])}
    out = {}
    for k, v in (raw_hint or {}).items():
        out[k] = v if n_by_strategy.get(k, 0) >= 3 else "hold"
    return out


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
    rc = _review_cfg()

    # #7 OOS 反过拟合：按日期切 train/test。AI 只看 train 派生 weight_hint/prompt_injection，
    # 代码用 test 做门控——train 里看着好但 test 里亏的策略，weight_hint 降级 hold，不反哺。
    long_entries = backtest.load_long_entries(ts_code=ts_code)
    train_entries, test_entries, cutoff = _oos_split(long_entries, rc["oos_test_days"])

    agg = backtest.aggregate(lookforward_days=lookforward_days, entries=train_entries)
    if agg["fillable_count"] == 0:
        raise BacktestReviewError("无可成交信号，无法复盘")
    # #2 样本量门槛：小样本复盘无统计意义，结论会误导反哺
    if agg["fillable_count"] < rc["min_review_samples"]:
        raise BacktestReviewError(
            f"train 可成交信号仅 {agg['fillable_count']} 笔，少于最小要求 "
            f"{rc['min_review_samples']}，复盘无统计意义"
        )

    # test 集独立算（用于 OOS 门控）；test 为空 → oos_insufficient，不门控但标注
    agg_test = None
    if test_entries:
        agg_test = backtest.aggregate(lookforward_days=lookforward_days, entries=test_entries)

    user_block = _format_stats(agg)
    if cutoff:
        user_block += (
            f"\n\n（以上为 TRAIN 期统计，cutoff={cutoff}；最近 {rc['oos_test_days']} 天作 OOS 验证集，"
            f"系统会用 OOS 集门控你的 weight_hint——样本内看着好但 OOS 亏的策略会被降级 hold。）"
        )
    client = make_client(api_key=api_key, base_url=cfg.get("deepseek", {}).get("base_url", "https://api.deepseek.com"))
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

    # #4a 强制样本门槛：n<3 的策略 weight_hint 一律 hold（代码兜底，不靠 AI 自律）
    hint_after_n_gate = _sanitize_weight_hint(agg, ai_part.get("strategy_weight_hint") or {})
    # #7 OOS 门控：用 test 集否决被样本内噪声误导的 hint
    weight_hint, oos_per_strategy = _gate_hints_with_oos(
        hint_after_n_gate, agg_test, rc["min_review_samples"])
    n_downgraded = sum(1 for p in oos_per_strategy if not p["survived_oos"])

    oos_validation = {
        "oos_test_days": rc["oos_test_days"],
        "cutoff": cutoff,
        "train_fillable": agg["fillable_count"],
        "test_fillable": agg_test["fillable_count"] if agg_test else 0,
        "oos_insufficient": (not agg_test) or agg_test["fillable_count"] < rc["min_review_samples"],
        "n_hints_downgraded": n_downgraded,
        "per_strategy": oos_per_strategy,
    }

    # 仅全局复盘（ts_code=None）落盘反哺：个股复盘(ts_code=X)只返回前端展示，
    # 不覆盖全局注入文件 / 策略统计（否则个股结论会污染全局反哺 —— 见 #3）。
    is_global = ts_code is None
    persisted = _persist_strategy_stats(agg, weight_hint) if is_global else None

    result = {
        "summary": ai_part.get("summary", ""),
        "findings": ai_part.get("findings") or [],
        "prompt_injection": ai_part.get("prompt_injection", ""),
        "strategy_weight_hint": weight_hint,
        "strategy_stats": persisted,
        "aggregate": agg,
        "oos_validation": oos_validation,
        "model": use_model,
        "generated_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
    }

    if is_global:
        # 落盘整份复盘，供 analyze 下次注入 prompt_injection（闭环最后一环）
        try:
            _review_path().write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"⚠ backtest_review 落盘失败: {e}")

    return result
