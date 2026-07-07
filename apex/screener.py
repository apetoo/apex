"""选股粗筛 — 多策略架构。

数据流：
  signals/ 5 个 fetcher 并发拉取 (raw signals)
    ↓
  strategies/ 4 个策略各自 select + score_one (规则层评分)
    ↓
  按策略权重融合 + 过滤 ST/小市值
    ↓
  Top N → AI 单只复核打分 (含 strategy_reasoning + 校准上下文)
    ↓
  写盘 ~/.stock-journal/screener/<YYYY-MM-DD>.jsonl

S2 起 strategy_weights 由 AI 看 regime + 历史胜率决定；S1 等权 1/N。
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import config, calibration
from apex.schemas import SignalRecord, ScreenerEntry
from apex.signals import FETCHERS
from apex.strategies import STRATEGIES

_TZ_CN = timezone(timedelta(hours=8))


# ── 日期辅助 ─────────────────────────────────────────────────────────────────

def _latest_trade_date() -> str:
    d = datetime.now(_TZ_CN).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


# ── 信号合并 ─────────────────────────────────────────────────────────────────

def _fetch_all_signals(trade_date: str, on_progress=None) -> dict[str, list[SignalRecord]]:
    results: dict[str, list[SignalRecord]] = {k: [] for k in FETCHERS}

    def _run(name: str):
        try:
            return name, FETCHERS[name](trade_date)
        except Exception:
            return name, []

    with ThreadPoolExecutor(max_workers=len(FETCHERS)) as pool:
        futures = {pool.submit(_run, name): name for name in FETCHERS}
        for fut in as_completed(futures):
            name, recs = fut.result()
            results[name] = recs or []
            if on_progress:
                on_progress(f"✓ 信号 {name}: {len(recs or [])} 条")
    return results


def _enrich_with_technical(by_source: dict, on_progress=None) -> dict:
    """Phase 2: 为近期涨停候选池批量拉取日线技术数据，注入 by_source['_technical']。

    所有技术面策略通过 by_source.get('_technical', {}) 读取共享缓存，
    避免每个策略各自调 API。
    """
    from apex import technical as tech

    limit_up_records = by_source.get("limit_up_history", []) or []
    ts_codes = [r["ts_code"] for r in limit_up_records if r.get("ts_code")]

    if not ts_codes:
        by_source["_technical"] = {}
        if on_progress:
            on_progress("⊘ 无近期涨停候选，跳过技术面数据拉取")
        return by_source

    if on_progress:
        on_progress(f"拉取 {len(ts_codes)} 只股票技术面数据（日线+均线+量比）...")

    tech.clear_cache()
    bars = tech.batch_fetch_bars(ts_codes)
    by_source["_technical"] = bars

    success = sum(1 for v in bars.values() if v is not None)
    if on_progress:
        on_progress(f"✓ 技术面数据就绪 {success}/{len(ts_codes)} 只（≥20根日线）")
    return by_source


# ── 策略层 ───────────────────────────────────────────────────────────────────

def _run_strategies(by_source: dict, weights: dict[str, float],
                    regime: Optional[dict] = None,
                    on_progress=None) -> dict[str, list[dict]]:
    by_strategy: dict[str, list[dict]] = {}
    for name, strategy in STRATEGIES.items():
        if weights.get(name, 0) <= 0:
            by_strategy[name] = []
            if on_progress:
                on_progress(f"⊘ 策略 {name} (权重 0，跳过)")
            continue
        try:
            cands = strategy.select(by_source, regime=regime) or []
        except Exception as e:
            if on_progress:
                on_progress(f"⚠ 策略 {name} select 失败: {e}")
            cands = []
        for c in cands:
            try:
                c["strategy"] = name
                c["strategy_score"] = float(strategy.score_one(c, regime=regime) or 0)
            except Exception:
                c["strategy_score"] = 0.0
        by_strategy[name] = sorted(cands, key=lambda x: -x.get("strategy_score", 0))
        if on_progress:
            on_progress(f"✓ 策略 {name}: {len(cands)} 候选")
    return by_strategy


def _merge_with_weights(by_strategy: dict[str, list[dict]],
                        weights: dict[str, float],
                        top_n_per_strategy: int) -> dict[str, dict]:
    """同股多策略命中时保留 weighted_score 最高的策略归属（D4 single attribution）。"""
    merged: dict[str, dict] = {}
    for name, cands in by_strategy.items():
        w = float(weights.get(name, 0) or 0)
        if w <= 0:
            continue
        for c in cands[:top_n_per_strategy]:
            ts = c["ts_code"]
            weighted = float(c.get("strategy_score", 0)) * w
            existing = merged.get(ts)
            if existing is None or weighted > existing["_weighted_score"]:
                new_c = dict(c)
                new_c["_weighted_score"] = round(weighted, 4)
                merged[ts] = new_c
    return merged


# ── 过滤 ─────────────────────────────────────────────────────────────────────

def _is_st(name: str) -> bool:
    n = (name or "").upper()
    return n.startswith("ST") or n.startswith("*ST") or "ST" in n[:4]


def _apply_filters(candidates: dict[str, dict], cfg: dict) -> dict[str, dict]:
    filters = cfg["screener"].get("filters", {}) or {}
    if not filters:
        return candidates

    out: dict[str, dict] = {}
    for ts_code, c in candidates.items():
        if filters.get("exclude_st") and _is_st(c.get("name", "")):
            continue
        min_mv_yi = filters.get("min_float_mv_yi")
        if min_mv_yi:
            mv_yuan = None
            for s in c.get("signals", []):
                raw = s.get("raw", {}) or {}
                if "float_mv" in raw and raw["float_mv"]:
                    mv_yuan = float(raw["float_mv"])
                    break
            if mv_yuan is not None and mv_yuan < min_mv_yi * 1e8:
                continue
        out[ts_code] = c
    return out


# ── AI 粗筛 ──────────────────────────────────────────────────────────────────

def _load_screener_prompt(regime: Optional[dict] = None) -> str:
    cfg = config.get()
    p = Path(cfg["paths"].get("screener_prompt_file", "apex/prompts/screener_quick.md"))
    if not p.is_absolute():
        p = Path(__file__).parent.parent / p
    if p.exists():
        base = p.read_text(encoding="utf-8")
    else:
        base = (
            "你是 A 股资深复盘交易员，60 秒判断单只票。"
            "结合给定的策略上下文 + 信号集，返回结构化评分。"
        )
    base = calibration.inject_into(base)
    if regime:
        from apex import regime as regime_mod
        base = base.rstrip() + f"\n\n## 当前市场 regime\n{regime_mod.format_for_prompt(regime)}\n"
    return base


def _format_signals_for_prompt(c: dict) -> str:
    """喂给 AI 的单只票上下文：策略归因优先，原始信号附后作为证据。"""
    lines = [
        f"股票：{c['ts_code']} {c.get('name', '')}",
        f"策略归属：{c.get('strategy', '?')}",
        f"策略归因：{c.get('strategy_reasoning', '?')}",
        f"规则评分：{c.get('strategy_score', 0):.2f}",
        "",
        "原始信号（证据）：",
    ]
    for s in c.get("signals", []):
        st = s.get("signal_type", "?")
        raw = s.get("raw", {}) or {}
        if st == "dragon_tiger":
            net = (raw.get("net_amount", 0) or 0) / 1e8
            buy = "净买入" if raw.get("is_net_buy") else "净卖出"
            reasons_text = "、".join(str(r)[:40] for r in (raw.get("reasons") or [])[:3])
            lines.append(f"  - 龙虎榜：{buy} {abs(net):.2f}亿 ｜ 席位 {raw.get('seats_count', 0)} ｜ {reasons_text}")
        elif st == "limit_up":
            lt = raw.get("limit_times", 1)
            ot = raw.get("open_times", 0)
            ind = raw.get("industry", "")
            fd = (raw.get("fd_amount", 0) or 0) / 1e8
            tr = raw.get("turnover_ratio", 0) or 0
            lines.append(f"  - 涨停板：{lt} 连板，炸板 {ot} 次，封单 {fd:.2f}亿，换手 {tr:.1f}%，行业 {ind}")
        elif st == "industry":
            ind = raw.get("industry_name", "")
            i_pct = raw.get("industry_pct", 0)
            s_pct = raw.get("stock_pct", 0)
            lines.append(f"  - 强势行业：{ind} 涨 {i_pct}% ｜ 个股涨 {s_pct}%")
        elif st == "northbound":
            inflow = (raw.get("inflow_mv", 0) or 0) / 1e8
            ind = raw.get("industry", "")
            lines.append(f"  - 北向资金：净增持 {inflow:.2f}亿 ｜ 行业 {ind}")
        elif st == "concept":
            cn = raw.get("lead_concept", "")
            cp = raw.get("concept_pct", 0)
            sp = raw.get("stock_pct", 0)
            all_c = (raw.get("all_concepts") or [])[:3]
            lines.append(f"  - 概念龙头：{cn} 涨 {cp}% ｜ 个股 {sp}% ｜ 命中题材 {','.join(all_c)}")
        elif st == "moneyflow":
            consec = raw.get("consec_days", 0)
            inflow = (raw.get("cum_net_inflow", 0) or 0) / 1e8
            cp = raw.get("cum_pct", 0)
            mp = raw.get("max_daily_pct", 0)
            vr = raw.get("vol_ratio", 0) or 0
            lines.append(f"  - 主力偷买：连续 {consec} 日净流入累计 {inflow:.2f}亿 ｜ 期间涨 {cp}% ｜ 单日最高 {mp}% ｜ 量比 {vr:.2f}")
    return "\n".join(lines)


_AI_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record_quick_screen",
        "description": "记录粗筛打分。必须调用此工具结束。",
        "parameters": {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "description": "1-10，越高越值得深分析"},
                "verdict": {
                    "type": "string",
                    "enum": ["看多", "偏多", "观望偏多", "中性", "观望偏空", "偏空", "看空"],
                },
                "actionable": {
                    "type": "string",
                    "enum": ["high", "medium", "low", "none"],
                    "description": "次日实际能买到的可能性。一字板 / 4+ 连板 = none；首板高炸板 = low；启动前夜或机构资金 = high。",
                },
                "entry_strategy": {
                    "type": "string",
                    "description": "次日入场策略，≤30 字。如：'回踩 5 日线买'/'竞价 +3% 内追'/'放弃，已锁盘'。",
                },
                "one_liner": {"type": "string", "description": "≤30字判断要点"},
                "red_flag": {"type": "boolean", "description": "明显风险（高位天量阴线/利空等）"},
            },
            "required": ["score", "verdict", "actionable", "entry_strategy", "one_liner", "red_flag"],
        },
    },
}


def _ai_screen_one(client, model: str, system: str, candidate: dict) -> Optional[dict]:
    user = _format_signals_for_prompt(candidate) + (
        "\n\n请基于以上策略归因 + 原始信号给出 1-10 分粗筛判断，"
        "并明确判断 actionable（次日能不能买到）和 entry_strategy（怎么买）。"
        "最后必须调用 record_quick_screen 工具记录结论。"
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=[_AI_TOOL_SCHEMA],
            max_tokens=4096,
            reasoning_effort="medium",
            extra_body={"thinking": {"type": "enabled"}},
        )
    except Exception:
        return None

    try:
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return None
        tc = msg.tool_calls[0]
        return json.loads(tc.function.arguments)
    except Exception:
        return None


def _ai_quick_screen(top_candidates: list[dict], cfg: dict,
                      on_progress=None, regime: Optional[dict] = None) -> list[dict]:
    from apex.llm import make_client

    client = make_client(
        api_key=cfg["deepseek"]["api_key"],
        base_url=cfg["deepseek"].get("base_url", "https://api.deepseek.com"),
    )
    model = cfg["screener"].get("ai_model") or cfg["deepseek"]["model"]
    system = _load_screener_prompt(regime=regime)
    concurrency = int(cfg["screener"].get("concurrency", 8))

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(_ai_screen_one, client, model, system, c): c
            for c in top_candidates
        }
        for i, fut in enumerate(as_completed(futures), start=1):
            c = futures[fut]
            ai = fut.result()
            entry = dict(c)
            if ai:
                entry["ai_score"] = int(ai.get("score") or 0)
                entry["verdict"] = ai.get("verdict")
                entry["actionable"] = ai.get("actionable") or "medium"
                entry["entry_strategy"] = ai.get("entry_strategy") or ""
                entry["one_liner"] = ai.get("one_liner") or ""
                entry["red_flag"] = bool(ai.get("red_flag"))
            else:
                entry["ai_score"] = None
                entry["verdict"] = None
                entry["actionable"] = None
                entry["entry_strategy"] = ""
                entry["one_liner"] = "AI 调用失败"
                entry["red_flag"] = False
            results.append(entry)
            if on_progress:
                on_progress(f"AI 打分 {i}/{len(top_candidates)}: {entry['ts_code']}")

    return results


# ── 策略权重 ─────────────────────────────────────────────────────────────────

def default_weights() -> dict[str, float]:
    """S1 等权；S2 起由 AI 选策略覆盖。"""
    n = len(STRATEGIES)
    return {name: round(1.0 / n, 4) for name in STRATEGIES}


# ── 写盘 ─────────────────────────────────────────────────────────────────────

def _write_log(trade_date: str,
               top_scored: list[dict],
               merged: dict[str, dict],
               by_strategy: dict[str, list[dict]],
               strategy_weights: dict[str, float],
               regime_data: Optional[dict] = None,
               weights_source: str = "equal",
               selector_reasoning: str = "") -> Path:
    cfg = config.get()
    out_dir = Path(cfg["paths"]["screener_dir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    iso = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    out_path = out_dir / f"{iso}.jsonl"

    by_strategy_summary = {
        name: {
            "n_candidates": len(cands),
            "weight": strategy_weights.get(name, 0),
            "top_5": [
                {"ts_code": c["ts_code"], "name": c.get("name", ""),
                 "strategy_score": c.get("strategy_score", 0),
                 "reasoning": c.get("strategy_reasoning", "")}
                for c in cands[:5]
            ],
        }
        for name, cands in by_strategy.items()
    }

    payload = {
        "trade_date": iso,
        "generated_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
        "regime": regime_data,
        "strategy_weights": strategy_weights,
        "weights_source": weights_source,
        "selector_reasoning": selector_reasoning,
        "by_strategy": by_strategy_summary,
        "top_scored": top_scored,
        "all_candidates_summary": [
            {
                "ts_code": c["ts_code"],
                "name": c.get("name", ""),
                "strategy": c.get("strategy"),
                "strategy_score": c.get("strategy_score", 0),
                "weighted_score": c.get("_weighted_score", 0),
            }
            for c in sorted(merged.values(), key=lambda x: -x.get("_weighted_score", 0))
        ],
        "stats": {
            "total_candidates": len(merged),
            "top_n": len(top_scored),
        },
    }

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    return out_path


# ── 主入口 ───────────────────────────────────────────────────────────────────

def run(
    trade_date: Optional[str] = None,
    on_progress=None,
    skip_ai: bool = False,
    strategy_weights: Optional[dict[str, float]] = None,
    skip_selector: bool = False,
) -> dict:
    """执行当日粗筛。

    strategy_weights: 外部传入则跳过 AI selector 直接用
    skip_selector: 强制跳过 AI selector，回落等权（用于离线 / 调试）
    """
    cfg = config.get()
    if not trade_date:
        trade_date = _latest_trade_date()
    trade_date = trade_date.replace("-", "")

    weights_source = "manual"
    selector_reasoning = ""

    if on_progress:
        on_progress(f"开始拉取 {len(FETCHERS)} 个信号源 (trade_date={trade_date})")
    by_source = _fetch_all_signals(trade_date, on_progress=on_progress)

    # Phase 2: 技术面数据（为近期涨停候选池批量拉日线）
    by_source = _enrich_with_technical(by_source, on_progress=on_progress)

    # NEW: regime 采集（用 by_source 复用 limit_up_count，避免重调 limit_list_d）
    regime_data = None
    try:
        from apex import regime as _regime
        regime_data = _regime.get(trade_date, by_source=by_source)
        if on_progress:
            on_progress(f"✓ regime: {regime_data.get('label')} ｜ {regime_data.get('summary', '')[:80]}")
    except Exception as e:
        if on_progress:
            on_progress(f"⚠ regime 采集失败: {e}")

    # 第一遍：用等权重跑所有策略，拿到候选数 → 给 selector 做决策
    placeholder_weights = default_weights()
    if on_progress:
        on_progress(f"按 {len(STRATEGIES)} 策略筛选...")
    by_strategy = _run_strategies(
        by_source, placeholder_weights, regime=regime_data, on_progress=on_progress,
    )
    candidates_count = {n: len(c) for n, c in by_strategy.items()}

    # 决定最终权重
    if strategy_weights is not None:
        weights_source = "manual"
        selector_reasoning = "调用方传入 strategy_weights，跳过 AI selector"
        final_weights = strategy_weights
    elif skip_selector:
        weights_source = "equal"
        selector_reasoning = "skip_selector=True，等权回落"
        final_weights = default_weights()
    else:
        try:
            from apex import strategy_selector
            final_weights, selector_reasoning = strategy_selector.select(
                regime_data, candidates_count, on_progress=on_progress,
            )
            weights_source = "ai"
        except Exception as e:
            if on_progress:
                on_progress(f"⚠ selector 异常: {e}，等权回落")
            final_weights = default_weights()
            weights_source = "equal_fallback"
            selector_reasoning = f"selector 异常: {e}"

    top_per_strat = int(cfg["screener"].get("top_n_per_strategy", 8))
    merged = _merge_with_weights(by_strategy, final_weights, top_per_strat)
    if on_progress:
        on_progress(f"融合后 {len(merged)} 只候选（去重后保留单策略归属）")

    merged = _apply_filters(merged, cfg)

    top_n = int(cfg["screener"].get("top_n_for_ai", 15))
    top_candidates = sorted(merged.values(), key=lambda x: -x.get("_weighted_score", 0))[:top_n]

    if cfg["screener"].get("ai_enabled", True) and not skip_ai and top_candidates:
        if on_progress:
            on_progress(f"AI 复核 Top {len(top_candidates)} 只...")
        top_scored = _ai_quick_screen(top_candidates, cfg, on_progress=on_progress, regime=regime_data)
        top_scored.sort(
            key=lambda x: (
                {"high": 3, "medium": 2, "low": 1, "none": 0}.get(x.get("actionable") or "medium", 2),
                x.get("ai_score") or 0,
            ),
            reverse=True,
        )
    else:
        top_scored = []
        for c in top_candidates:
            entry = dict(c)
            entry.update({
                "ai_score": None, "verdict": None, "actionable": None,
                "entry_strategy": "", "one_liner": "", "red_flag": False,
            })
            top_scored.append(entry)

    out_path = _write_log(
        trade_date, top_scored, merged, by_strategy, final_weights,
        regime_data=regime_data,
        weights_source=weights_source,
        selector_reasoning=selector_reasoning,
    )
    if on_progress:
        on_progress(f"✓ 写入 {out_path}")

    return {
        "trade_date": trade_date,
        "out_path": str(out_path),
        "top_scored": top_scored,
        "by_strategy": candidates_count,
        "strategy_weights": final_weights,
        "weights_source": weights_source,
        "selector_reasoning": selector_reasoning,
        "regime": regime_data,
        "total_candidates": len(merged),
    }


def load_latest() -> Optional[dict]:
    cfg = config.get()
    out_dir = Path(cfg["paths"]["screener_dir"]).expanduser()
    if not out_dir.exists():
        return None
    files = sorted(out_dir.glob("*.jsonl"), reverse=True)
    if not files:
        return None
    with open(files[0], encoding="utf-8") as f:
        line = f.readline().strip()
    return json.loads(line) if line else None


def load_by_date(iso_date: str) -> Optional[dict]:
    cfg = config.get()
    out_dir = Path(cfg["paths"]["screener_dir"]).expanduser()
    p = out_dir / f"{iso_date}.jsonl"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        line = f.readline().strip()
    return json.loads(line) if line else None


def list_available_dates() -> list[str]:
    cfg = config.get()
    out_dir = Path(cfg["paths"]["screener_dir"]).expanduser()
    if not out_dir.exists():
        return []
    return sorted([p.stem for p in out_dir.glob("*.jsonl")], reverse=True)


if __name__ == "__main__":
    import sys
    td = sys.argv[1] if len(sys.argv) > 1 else None
    skip_ai = "--no-ai" in sys.argv
    res = run(trade_date=td, on_progress=lambda m: print(m), skip_ai=skip_ai)
    print(f"\n完成。{res['total_candidates']} 只候选 → Top {len(res['top_scored'])}")
    print(f"按策略候选数: {res['by_strategy']}")
    print(f"输出：{res['out_path']}")
