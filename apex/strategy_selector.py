"""AI 策略选择器 —— 看 regime + 历史胜率 + 今日候选数 → 输出策略权重。

约束：
  - sum(weights) = 1（代码强制归一化）
  - 单策略 [_MIN_WEIGHT, _MAX_WEIGHT]，0 表示显式 disable
  - AI 调用失败 / 返回不合规 → fallback 等权重

数据流：
  regime.get(trade_date)
    + calibration.load().by_strategy
    + STRATEGIES (NAME / DESCRIPTION)
    + by_strategy_count (今日各策略候选数)
      ↓
  AI prompt → tool select_strategies(weights, reasoning, primary_strategy)
      ↓
  归一化 + 边界裁剪 → {name: weight}, reasoning
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from openai import OpenAI

from apex import calibration, config, regime as regime_mod
from apex.strategies import STRATEGIES

_TZ_CN = timezone(timedelta(hours=8))

_MIN_WEIGHT = 0.05
_MAX_WEIGHT = 0.6


def _strategy_names() -> list[str]:
    return list(STRATEGIES.keys())


def _build_tool_schema() -> dict:
    weight_props = {
        name: {"type": "number", "description": f"策略 {name} 权重 0-1"}
        for name in _strategy_names()
    }
    return {
        "type": "function",
        "function": {
            "name": "select_strategies",
            "description": f"决定今日 {len(_strategy_names())} 个策略的权重分配。必须调用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "weights": {
                        "type": "object",
                        "properties": weight_props,
                        "required": _strategy_names(),
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "30-100 字解释为何这样分配（不能套话，要引用 regime / 胜率 / 候选数）",
                    },
                    "primary_strategy": {
                        "type": "string",
                        "enum": _strategy_names(),
                        "description": "今日权重最高的策略",
                    },
                },
                "required": ["weights", "reasoning", "primary_strategy"],
            },
        },
    }


def _format_strategies_block() -> str:
    parts = []
    for name in _strategy_names():
        s = STRATEGIES[name]
        parts.append(f"### {name}\n{s.DESCRIPTION}")
    return "\n\n".join(parts)


def _format_history_block(regime_label: Optional[str] = None) -> str:
    cal = calibration.load() or {}
    by_strat = cal.get("by_strategy") or {}
    by_sxr = cal.get("by_strategy_x_regime") or {}
    if not by_strat and not by_sxr:
        return "（暂无历史数据 —— 所有策略冷启动，按 regime + 候选数偏置即可）"

    parts = []

    parts.append("**全样本（所有 regime 合并）**")
    if by_strat:
        rows = ["| 策略 | n | 胜率 | 平均 P&L | 最差 |", "|---|---|---|---|---|"]
        for name in _strategy_names():
            b = by_strat.get(name)
            if not b or (b.get("n") or 0) == 0:
                rows.append(f"| {name} | 0 | — | — | — |")
            else:
                worst = b.get("worst_pnl_pct")
                worst_str = f"{worst * 100:+.2f}%" if worst is not None else "—"
                rows.append(
                    f"| {name} | {b['n']} | {b['win_rate'] * 100:.0f}% | "
                    f"{b['avg_pnl_pct'] * 100:+.2f}% | {worst_str} |"
                )
        parts.append("\n".join(rows))
    else:
        parts.append("（无）")

    if regime_label:
        parts.append(f"\n**当前 regime ({regime_label}) 下**")
        if by_sxr:
            rows = ["| 策略 | n | 胜率 | 平均 P&L | 最差 |", "|---|---|---|---|---|"]
            saw_any = False
            for name in _strategy_names():
                key = f"{name}@{regime_label}"
                b = by_sxr.get(key)
                if not b or (b.get("n") or 0) == 0:
                    rows.append(f"| {name} | 0 | — | — | — |")
                else:
                    saw_any = True
                    worst = b.get("worst_pnl_pct")
                    worst_str = f"{worst * 100:+.2f}%" if worst is not None else "—"
                    rows.append(
                        f"| {name} | {b['n']} | {b['win_rate'] * 100:.0f}% | "
                        f"{b['avg_pnl_pct'] * 100:+.2f}% | {worst_str} |"
                    )
            if saw_any:
                rows.append("（注意：n < 5 的桶噪音大，决策时应回退到全样本）")
            else:
                rows.append("（当前 regime 下样本不足，回退看全样本）")
            parts.append("\n".join(rows))
        else:
            parts.append("（暂无 strategy×regime 数据，回退看全样本）")

    return "\n".join(parts)


def _format_candidates_block(counts: dict[str, int]) -> str:
    return "\n".join(
        f"- {name}: {counts.get(name, 0)} 候选"
        for name in _strategy_names()
    )


def _build_prompt(regime: Optional[dict], counts: dict[str, int]) -> str:
    regime_label = (regime or {}).get("label")
    return f"""你是 A 股选股策略的总指挥。今天需要决定各策略的权重分配，让 screener 按权重融合候选股。

# 今日市场 regime
{regime_mod.format_for_prompt(regime)}

# 各策略说明
{_format_strategies_block()}

# 各策略历史表现（按用户的 closed_positions 算）
{_format_history_block(regime_label=regime_label)}

# 各策略今日候选数（信号面）
{_format_candidates_block(counts)}

# 决策原则
1. **regime 影响策略偏好**（先验，无数据时主要靠这个）：
   - `risk_on` → 偏好 `first_board_leader` / `leader_with_volume`（情绪驱动型）
   - `risk_off` → 偏好 `institutional_flow`（防御型，机构持仓周期长）
   - `neutral` → 偏好 `industry_rotation`（板块效应）
2. **历史胜率优先级**：
   - 优先看"当前 regime 下"的桶（如果 n ≥ 5）
   - n < 5 时退回看全样本
   - 全样本也 < 5 时全凭 regime 先验
   - 胜率 < 40% 的桶应主动砍权重；胜率 > 60% 的桶可适度上调
3. **候选数约束**：候选 0 的策略 weight = 0；候选 > 30 的策略说明信号噪声大，权重不宜超 0.4
4. **权重边界**：单策略 [{_MIN_WEIGHT}, {_MAX_WEIGHT}]（0 = 显式 disable）；和 = 1.0
5. **reasoning 要具体**：必须引用 regime 标签 / 具体桶的胜率（哪张表）/ 候选数，不要"综合考虑"这种套话

调用 `select_strategies` 工具输出 weights / reasoning / primary_strategy。"""


def _equal_weights() -> dict[str, float]:
    n = len(STRATEGIES)
    return {name: round(1.0 / n, 4) for name in STRATEGIES}


def _normalize_weights(raw: dict) -> dict[str, float]:
    """裁剪到 [MIN, MAX] + 缺策略补 0 + 归一化使和=1。"""
    cleaned: dict[str, float] = {}
    for name in _strategy_names():
        try:
            w = float(raw.get(name, 0) or 0)
        except (TypeError, ValueError):
            w = 0.0
        if w <= 0:
            cleaned[name] = 0.0
            continue
        if w < _MIN_WEIGHT:
            w = _MIN_WEIGHT
        if w > _MAX_WEIGHT:
            w = _MAX_WEIGHT
        cleaned[name] = w

    total = sum(cleaned.values())
    if total <= 0:
        return _equal_weights()
    return {n: round(w / total, 4) for n, w in cleaned.items()}


def select(regime: Optional[dict],
           candidates_count: dict[str, int],
           on_progress=None) -> tuple[dict[str, float], str]:
    """主入口。返回 (weights_dict, reasoning_text)。失败回落等权 + 注明原因。"""
    cfg = config.get()
    api_key = cfg.get("deepseek", {}).get("api_key", "") if cfg else ""
    if not api_key:
        if on_progress:
            on_progress("⚠ deepseek.api_key 缺失，等权回落")
        return _equal_weights(), "未配置 deepseek API key，使用等权重"

    use_model = (
        cfg.get("strategy_selector", {}).get("model")
        or cfg.get("screener", {}).get("ai_model")
        or "deepseek-chat"
    )

    # 候选全 0 → 不调用 AI
    total_cands = sum(candidates_count.values())
    if total_cands == 0:
        return _equal_weights(), "今日无候选，使用等权重（无影响）"

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    prompt = _build_prompt(regime, candidates_count)
    tool = _build_tool_schema()

    try:
        resp = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": "你是 A 股选股策略的总指挥。你必须调用 select_strategies 工具来输出权重，不要用文字代替。"},
                {"role": "user", "content": prompt},
            ],
            tools=[tool],
            max_tokens=4096,
            reasoning_effort="medium",
            extra_body={"thinking": {"type": "enabled"}},
        )
    except Exception as e:
        if on_progress:
            on_progress(f"⚠ AI 选策略失败: {type(e).__name__}: {e}，等权回落")
        return _equal_weights(), f"AI 调用失败 ({type(e).__name__}): {e}"

    try:
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return _equal_weights(), "AI 未调用工具，等权回落"
        tc = msg.tool_calls[0]
        args = json.loads(tc.function.arguments)
    except Exception as e:
        return _equal_weights(), f"AI 输出解析失败: {e}"

    raw_weights = args.get("weights") or {}
    weights = _normalize_weights(raw_weights)
    reasoning = (args.get("reasoning") or "").strip()
    primary = args.get("primary_strategy", "")

    # 候选 0 的策略强制 weight=0，再归一化
    forced_zero = False
    for name, cnt in candidates_count.items():
        if cnt == 0 and weights.get(name, 0) > 0:
            weights[name] = 0.0
            forced_zero = True
    if forced_zero:
        total = sum(weights.values())
        if total > 0:
            weights = {n: round(w / total, 4) for n, w in weights.items()}
        else:
            weights = _equal_weights()
        reasoning = (reasoning + " ｜ [系统校正：候选 0 的策略已强制权重 0]").strip()

    if on_progress:
        weight_str = ", ".join(f"{n}={w:.2f}" for n, w in weights.items())
        on_progress(f"✓ 策略权重 → {weight_str} ｜ primary={primary}")

    return weights, reasoning
