"""「我的交易系统」合成层（ADR-0001 / ADR-0002）。

把散落的 calibration / evidence_attribution / postmortem / closed_positions /
trades / journal 汇成一张「我的交易系统」视图。v1 是纪律镜像优先：
Trading DNA（描述）+ Behavior Analytics（可观测行为+代理情绪）+ AI Adherence
+ Discipline Score。Tier-2（Performance Attribution / Rule Discovery / Simulator）
在此层以「累积中」状态存在，n 达阈后再转结论。

输出 ~/.stock-journal/system.json。公共 API 仿 calibration：
  compute()  -- 重算并落盘，返回 dict
  load()     -- 读最近一份（不重算）

样本门控（ADR-0002）：每个指标带 n + confidence 标志：
  ok / low_sample_descriptive_only(n<20) / accumulating(n<30 的期望类) / no_data
页面据此渲染「累积中」，绝不把低 n 偶然盈亏包装成「你的优势」。
"""
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import config, journal, trades as _trades

_TZ_CN = timezone(timedelta(hours=8))

_ATTRIBUTION_N_MIN = 20   # 描述性归因可信阈
_EXPECTANCY_N_MIN = 30    # 期望/胜率结论可信阈


def _path() -> Path:
    cfg = config.get()
    return Path(cfg["paths"]["journal_dir"]).expanduser() / "system.json"


def _desc_flag(n: int) -> str:
    if n <= 0:
        return "no_data"
    return "ok" if n >= _ATTRIBUTION_N_MIN else "low_sample_descriptive_only"


def _exp_flag(n: int) -> str:
    if n <= 0:
        return "no_data"
    return "ok" if n >= _EXPECTANCY_N_MIN else "accumulating"


def _score(conditions: list) -> Optional[float]:
    """三态条件列表 -> 满足率（None 视为未覆盖，不计入分母）。"""
    vals = [c for c in conditions if c is not None]
    if not vals:
        return None
    return round(sum(1 for v in vals if v) / len(vals), 3)


def _ai_plan_for(ts_code: str, entry_date: Optional[str]) -> dict:
    """开仓时该股最近的 journal entry 的 price_advice（AI 计划）。无则 {}。"""
    if not entry_date:
        return {}
    try:
        entries = journal.load_verdicts(ts_code=ts_code)
    except Exception:
        return {}
    if not entries:
        return {}
    cutoff = entry_date + "T23:59:59"
    before = [e for e in entries
              if (e.get("analyzed_at") or e.get("date") or "") <= cutoff]
    if not before:
        return {}   # 入场前无分析 -> 无计划（不回退到入场后，避免 look-ahead bias）
    latest = sorted(before, key=lambda e: e.get("analyzed_at") or e.get("date") or "")[-1]
    return latest.get("price_advice") or {}


def _resolve_ai_plan(c: dict) -> dict:
    """取开仓时 AI 计划 price_advice。优先读 close 时烘焙的快照 open.ai_price_advice
    （确定、无 look-ahead、与 ai_verdict 同源）；老记录无快照时回退到 _ai_plan_for
    （仅入场前分析，不回退未来）。"""
    op = c.get("open") or {}
    pa = op.get("ai_price_advice")
    if pa and isinstance(pa, dict):
        return pa
    return _ai_plan_for(c.get("ts_code"), op.get("entry_date"))


def _hold_bucket(days: Optional[int]) -> str:
    if days is None:
        return "unknown"
    if days <= 1:
        return "0-1d"
    if days <= 5:
        return "2-5d"
    if days <= 20:
        return "6-20d"
    return "20+d"


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat((s[:10]))
    except Exception:
        return None


def compute() -> dict:
    """重算并写入 system.json，返回 dict。"""
    closed = watchlist_load_closed()
    all_trades = _safe(_trades.load_trades, [])
    all_entries = _safe(journal.load_entries, [])

    buys = [t for t in all_trades if t.get("side") == "buy"]
    closed_n = len(closed)
    trades_n = len(all_trades)
    journal_n = len(all_entries)
    eligible_n = sum(
        1 for c in closed
        if (c.get("open") or {}).get("ai_verdict") is not None
        and (c.get("close") or {}).get("realized_pnl_pct") is not None
    )

    dna = _trading_dna(closed)
    behavior = _behavior(closed, all_trades, buys)
    ai_adh = _ai_adherence(closed)
    discipline = _discipline(closed, ai_adh)

    out = {
        "computed_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
        "sample": {
            "closed_n": closed_n,
            "trades_n": trades_n,
            "journal_n": journal_n,
            "eligible_n": eligible_n,
        },
        "thresholds": {
            "attribution_n_min": _ATTRIBUTION_N_MIN,
            "expectancy_n_min": _EXPECTANCY_N_MIN,
        },
        "trading_dna": dna,
        "behavior": behavior,
        "ai_adherence": ai_adh,
        "discipline_score": discipline,
        # Tier-2：v1 以「累积中」状态存在，n 达阈后再展开（ADR-0002）
        "tier2_status": {
            "performance_attribution": _exp_flag(eligible_n),
            "rule_discovery": _exp_flag(eligible_n),
            "strategy_simulator": "accumulating" if eligible_n < _EXPECTANCY_N_MIN else "ok",
            "note": "n 达阈后转结论；当前仅描述性展示",
        },
    }
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    return out


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def watchlist_load_closed() -> list:
    from apex import watchlist as _wl
    try:
        return _wl.load_closed_positions()
    except Exception:
        return []


def _trading_dna(closed: list) -> dict:
    verdicts = Counter((c.get("open") or {}).get("ai_verdict") for c in closed)
    verdicts.pop(None, None)
    strategies = Counter((c.get("open") or {}).get("strategy") for c in closed)
    strategies.pop(None, None)
    setups = Counter((c.get("open") or {}).get("setup") for c in closed)
    setups.pop(None, None)
    regimes = Counter((c.get("open") or {}).get("regime_at_open") for c in closed)
    regimes.pop(None, None)
    sectors = Counter((c.get("open") or {}).get("sector") for c in closed)
    sectors.pop(None, None)

    hold_days = [(c.get("close") or {}).get("days_held") for c in closed]
    hold_days = [d for d in hold_days if isinstance(d, (int, float))]
    avg_hold = round(sum(hold_days) / len(hold_days), 1) if hold_days else None

    sector_total = sum(sectors.values())
    sector_top = None
    if sector_total > 0 and sectors:
        top_name, top_cnt = sectors.most_common(1)[0]
        sector_top = {"name": top_name, "count": top_cnt,
                      "share": round(top_cnt / sector_total, 3)}

    return {
        "verdict_distribution": dict(verdicts),
        "strategy_distribution": dict(strategies),
        "setup_distribution": dict(setups),                  # 前向捕获后才有数据
        "regime_distribution": dict(regimes),                # regime_at_open 回填后才有数据
        "sector_concentration": {                            # PR4: tushare 行业回填后有数据
            "top": sector_top,
            "distribution": dict(sectors),
            "n": sector_total,
            "confidence": _desc_flag(sector_total),
        },
        "avg_hold_days": {"value": avg_hold, "n": len(hold_days),
                          "confidence": _desc_flag(len(hold_days))},
    }


def _behavior(closed: list, all_trades: list, buys: list) -> dict:
    return {
        "chase": _chase(closed),
        "average_down": _average_down(buys),
        "stop_discipline": _stop_discipline(closed),
        "take_profit_discipline": _take_profit_discipline(closed),
        "hold_period_distribution": _hold_distribution(closed),
        "proxy_emotional": _proxy_emotional(closed, all_trades, buys),
    }


def _chase(closed: list) -> dict:
    """追高：入场时价 > MA5 且 price_vs_ma5_pct > 3%（用 AI 特征快照代理）。"""
    n = 0
    hit = 0
    for c in closed:
        feats = (c.get("open") or {}).get("ai_features") or {}
        pos = feats.get("ma5_position")
        pvma = feats.get("price_vs_ma5_pct")
        if pos is None or pvma is None:
            continue
        n += 1
        try:
            if pos == "above" and float(pvma) > 3:
                hit += 1
        except (TypeError, ValueError):
            continue
    return {"n": n, "count": hit, "rate": round(hit / n, 3) if n else None,
            "confidence": _desc_flag(n)}


def _average_down(buys: list) -> dict:
    """补仓：同股多笔 buy 且后续价低于前笔（向下摊平）。"""
    by_code: dict[str, list] = defaultdict(list)
    for t in buys:
        by_code[t.get("ts_code")].append(t)
    multi = {k: v for k, v in by_code.items() if len(v) > 1}
    avg_down_codes = 0
    for code, tlst in multi.items():
        tlst = sorted(tlst, key=lambda t: t.get("traded_at") or "")
        for i in range(1, len(tlst)):
            try:
                if float(tlst[i].get("fill_price") or 0) < float(tlst[i - 1].get("fill_price") or 0):
                    avg_down_codes += 1
                    break
            except (TypeError, ValueError):
                continue
    return {"n": len(multi), "count": avg_down_codes,
            "rate": round(avg_down_codes / len(multi), 3) if multi else None,
            "confidence": _desc_flag(len(multi))}


def _stop_discipline(closed: list) -> dict:
    no_stop = honored = override = not_triggered = 0
    n = 0
    for c in closed:
        op = c.get("open") or {}
        cl = c.get("close") or {}
        stop = op.get("stop_loss")
        low = cl.get("low_during_hold")
        if not stop or float(stop) <= 0:
            no_stop += 1
            continue
        n += 1
        breached = low is not None and float(low) <= float(stop)
        if not breached:
            not_triggered += 1
        elif cl.get("exit_reason") == "stop_hit":
            honored += 1
        else:
            override += 1   # 触及止损却 manual 出场 = 越线硬扛（纪律违规）
    return {"n": n, "no_stop_n": no_stop, "honored_n": honored,
            "override_breach_n": override, "not_triggered_n": not_triggered,
            "confidence": _desc_flag(n)}


def _take_profit_discipline(closed: list) -> dict:
    honored = missed = not_hit = no_target = 0
    n = 0
    for c in closed:
        op = c.get("open") or {}
        cl = c.get("close") or {}
        target = op.get("target")
        high = cl.get("high_during_hold")
        if not target or float(target) <= 0:
            no_target += 1
            continue
        n += 1
        reached = high is not None and float(high) >= float(target)
        if not reached:
            not_hit += 1
        elif cl.get("exit_reason") == "target_hit":
            honored += 1
        else:
            missed += 1     # 触及目标却没止盈 = 让利润回吐
    return {"n": n, "no_target_n": no_target, "honored_n": honored,
            "missed_n": missed, "not_hit_n": not_hit,
            "confidence": _desc_flag(n)}


def _hold_distribution(closed: list) -> dict:
    buckets = Counter(_hold_bucket((c.get("close") or {}).get("days_held")) for c in closed)
    return {"distribution": dict(buckets), "n": len(closed),
            "confidence": _desc_flag(len(closed))}


def _proxy_emotional(closed: list, all_trades: list, buys: list) -> dict:
    """情绪化拆成可观测代理（ADR-0001 Behavior Proxy）。"""
    # revenge：上一笔实现亏损后 ≤3 天内又开仓
    loss_dates = [_parse_date((c.get("close") or {}).get("exit_date"))
                  for c in closed
                  if (c.get("close") or {}).get("realized_pnl_pct") is not None
                  and float((c.get("close") or {}).get("realized_pnl_pct") or 0) < 0]
    loss_dates = [d for d in loss_dates if d]
    revenge_n = 0
    for t in buys:
        td = _parse_date(t.get("traded_at"))
        if not td:
            continue
        if any(0 <= (td - ld).days <= 3 for ld in loss_dates):
            revenge_n += 1
    revenge_rate = round(revenge_n / len(buys), 3) if buys else None

    # fomo：入场价 > AI plan entry × 1.05（追在 AI 建议入场点之上 5%）
    fomo_n = 0
    fomo_denom = 0
    for c in closed:
        op = c.get("open") or {}
        fill = op.get("actual_fill_price") or op.get("entry_price")
        plan = _resolve_ai_plan(c)
        ai_entry = (plan or {}).get("entry")
        if not fill or not ai_entry or float(ai_entry) <= 0:
            continue
        fomo_denom += 1
        try:
            if float(fill) > float(ai_entry) * 1.05:
                fomo_n += 1
        except (TypeError, ValueError):
            continue
    fomo_rate = round(fomo_n / fomo_denom, 3) if fomo_denom else None

    # overtrading：单日交易数 > 3 的交易日占比
    day_counts = Counter((t.get("traded_at") or "")[:10] for t in all_trades if t.get("traded_at"))
    overtrading_days = sum(1 for _, cnt in day_counts.items() if cnt > 3)
    overtrading_rate = round(overtrading_days / len(day_counts), 3) if day_counts else None

    return {
        "revenge": {"n": len(buys), "count": revenge_n, "rate": revenge_rate,
                    "confidence": _desc_flag(len(buys))},
        "fomo": {"n": fomo_denom, "count": fomo_n, "rate": fomo_rate,
                 "confidence": _desc_flag(fomo_denom)},
        "overtrading": {"n": len(day_counts), "count": overtrading_days,
                        "rate": overtrading_rate,
                        "confidence": _desc_flag(len(day_counts))},
    }


def _ai_adherence(closed: list) -> dict:
    """守 AI 计划：入场带 / 止损设否 / 止损执行。每笔 + 聚合。"""
    per_trade = []
    for c in closed:
        op = c.get("open") or {}
        cl = c.get("close") or {}
        fill = op.get("actual_fill_price") or op.get("entry_price")
        plan = _resolve_ai_plan(c)
        ai_entry = (plan or {}).get("entry")
        stop = op.get("stop_loss")
        low = cl.get("low_during_hold")

        entry_band_ok = None
        if fill and ai_entry and float(ai_entry) > 0:
            try:
                entry_band_ok = abs(float(fill) - float(ai_entry)) / float(ai_entry) <= 0.02
            except (TypeError, ValueError):
                entry_band_ok = None

        stop_set = bool(stop and float(stop) > 0) if stop is not None else False
        stop_honored = None
        if stop_set and low is not None:
            breached = float(low) <= float(stop)
            if breached:
                stop_honored = cl.get("exit_reason") == "stop_hit"
            else:
                stop_honored = True   # 未触及，无违规

        per_trade.append({
            "ts_code": c.get("ts_code"),
            "name": c.get("name"),
            "entry_band_ok": entry_band_ok,
            "stop_set": stop_set,
            "stop_honored": stop_honored,
            "has_ai_plan": bool(plan and plan.get("entry")),  # 有可用 AI 入场计划（看多判断且 entry>0）
            "score": _score([entry_band_ok, stop_set, stop_honored]),
        })

    n = len(per_trade)
    agg = {
        "entry_band_rate": _rate(per_trade, "entry_band_ok"),
        "stop_set_rate": _rate(per_trade, "stop_set"),
        "stop_honored_rate": _rate(per_trade, "stop_honored"),
        "n": n,
        "confidence": _desc_flag(n),
    }
    return {"per_trade": per_trade, "aggregate": agg}


def _rate(per_trade: list, key: str) -> Optional[float]:
    vals = [pt.get(key) for pt in per_trade if pt.get(key) is not None]
    if not vals:
        return None
    return round(sum(1 for v in vals if v) / len(vals), 3)


def _discipline(closed: list, ai_adh: dict) -> dict:
    """Discipline Score：v1 = AI 计划守规分（rule_checklist 有数据后混入自录层）。"""
    per_trade = []
    for c, pt in zip(closed, ai_adh.get("per_trade", [])):
        conds = [pt.get("entry_band_ok"), pt.get("stop_set"), pt.get("stop_honored")]
        # 自录 Rule 检查表（前向捕获后才有）
        rule = (c.get("open") or {}).get("rule_checklist") or {}
        for item in (rule.get("items") or []):
            if item.get("checked") is not None:
                conds.append(bool(item.get("checked")))
        per_trade.append({
            "ts_code": c.get("ts_code"),
            "name": c.get("name"),
            "score": _score(conds),
            "has_user_rule": bool(rule.get("items")),
        })
    scores = [pt["score"] for pt in per_trade if pt.get("score") is not None]
    rolling = round(sum(scores) / len(scores), 3) if scores else None
    return {
        "per_trade": per_trade,
        "rolling_avg": {"value": rolling, "n": len(scores),
                        "confidence": _desc_flag(len(scores))},
    }


def load() -> Optional[dict]:
    p = _path()
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
