"""校准模块 —— 聚合 closed_positions.jsonl，按桶算实际胜率，与 AI 自报 confidence 对照。

输出 ~/.stock-journal/calibration.json，结构：
  {
    "computed_at": "...",
    "total_closed": 23,
    "eligible_for_calibration": 18,    # 有 ai_verdict + ai_confidence + realized_pnl 的笔数
    "by_bucket": {
      "看多@7-10": {n: 8, wins: 3, losses: 5, win_rate: 0.375, avg_pnl_pct: -0.012, ...}
    },
    "by_signal_combo": {
      "dragon_tiger+industry": {n: 5, wins: 3, win_rate: 0.6, ...}
    }
  }

公共 API：
  compute()                                       —— 重新聚合并落盘，返回 dict
  load()                                          —— 读最近一份结果（不重算）
  calibrate_confidence(raw, verdict, n_min=5)     —— 校准单次 AI confidence → (score, why)
  format_for_prompt(n_min=3)                      —— markdown 表格，准备塞进 system prompt（Phase 1.6 用）

设计：
  - "胜"判定：realized_pnl_pct > 0.5%（死区，避免抖动当成胜负）
  - confidence 桶：1-3 / 4-6 / 7-10（与 postmortem._bucket_for 一致）
  - 信号组合：sorted(open.screener_signals).join("+")，缺数据则不进 combo 统计
  - 写盘 raise 给调用方处理（fail-loud），读盘缺失返回 None（fail-soft）
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import config

_TZ_CN = timezone(timedelta(hours=8))

_WIN_THRESHOLD = 0.005    # +0.5% 以上算胜
_LOSS_THRESHOLD = -0.005  # -0.5% 以下算败


def _path() -> Path:
    cfg = config.get()
    return Path(cfg["paths"]["journal_dir"]).expanduser() / "calibration.json"


def _bucket_for(conf: int) -> str:
    if conf <= 3:
        return "1-3"
    if conf <= 6:
        return "4-6"
    return "7-10"


def _classify_key(pnl: float) -> str:
    """返回累计 dict 的 key（注意 'loss' 不能简单 +s）。"""
    if pnl > _WIN_THRESHOLD:
        return "wins"
    if pnl < _LOSS_THRESHOLD:
        return "losses"
    return "breakevens"


def compute() -> dict:
    """重算并写入 calibration.json，返回 calibration dict。"""
    from apex import watchlist as _wl
    records = _wl.load_closed_positions()

    by_bucket: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0, "breakevens": 0,
        "pnl_sum": 0.0, "pnl_min": None, "pnl_max": None,
    })
    by_combo: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0, "breakevens": 0, "pnl_sum": 0.0,
    })
    by_strategy: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0, "breakevens": 0,
        "pnl_sum": 0.0, "pnl_min": None, "pnl_max": None,
    })
    by_strategy_x_regime: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0, "breakevens": 0,
        "pnl_sum": 0.0, "pnl_min": None, "pnl_max": None,
    })

    total = 0
    eligible = 0
    for r in records:
        total += 1
        o = r.get("open") or {}
        c = r.get("close") or {}
        verdict = o.get("ai_verdict")
        conf = o.get("ai_confidence")
        pnl = c.get("realized_pnl_pct")
        if verdict is None or conf is None or pnl is None:
            continue
        try:
            conf_int = int(conf)
            pnl_float = float(pnl)
        except (TypeError, ValueError):
            continue
        eligible += 1

        bucket_key = f"{verdict}@{_bucket_for(conf_int)}"
        b = by_bucket[bucket_key]
        b["n"] += 1
        b["pnl_sum"] += pnl_float
        b["pnl_min"] = pnl_float if b["pnl_min"] is None else min(b["pnl_min"], pnl_float)
        b["pnl_max"] = pnl_float if b["pnl_max"] is None else max(b["pnl_max"], pnl_float)
        cls_key = _classify_key(pnl_float)
        b[cls_key] += 1

        signals = o.get("screener_signals")
        if signals and isinstance(signals, list):
            combo_key = "+".join(sorted(set(str(s) for s in signals)))
            if combo_key:
                sc = by_combo[combo_key]
                sc["n"] += 1
                sc["pnl_sum"] += pnl_float
                sc[cls_key] += 1

        strat = o.get("strategy")
        if strat:
            ss = by_strategy[str(strat)]
            ss["n"] += 1
            ss["pnl_sum"] += pnl_float
            ss[cls_key] += 1
            ss["pnl_min"] = pnl_float if ss["pnl_min"] is None else min(ss["pnl_min"], pnl_float)
            ss["pnl_max"] = pnl_float if ss["pnl_max"] is None else max(ss["pnl_max"], pnl_float)

            regime_label = o.get("regime_at_open")
            if regime_label:
                key = f"{strat}@{regime_label}"
                sxr = by_strategy_x_regime[key]
                sxr["n"] += 1
                sxr["pnl_sum"] += pnl_float
                sxr[cls_key] += 1
                sxr["pnl_min"] = pnl_float if sxr["pnl_min"] is None else min(sxr["pnl_min"], pnl_float)
                sxr["pnl_max"] = pnl_float if sxr["pnl_max"] is None else max(sxr["pnl_max"], pnl_float)

    def _finalize_bucket(b: dict, with_extremes: bool) -> dict:
        n = b["n"]
        out = {
            "n": n,
            "wins": b["wins"],
            "losses": b["losses"],
            "breakevens": b["breakevens"],
            "win_rate": round(b["wins"] / n, 3) if n else 0.0,
            "avg_pnl_pct": round(b["pnl_sum"] / n, 4) if n else 0.0,
        }
        if with_extremes:
            out["best_pnl_pct"] = round(b["pnl_max"], 4) if b["pnl_max"] is not None else None
            out["worst_pnl_pct"] = round(b["pnl_min"], 4) if b["pnl_min"] is not None else None
        return out

    out = {
        "computed_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
        "total_closed": total,
        "eligible_for_calibration": eligible,
        "by_bucket": {k: _finalize_bucket(v, True) for k, v in by_bucket.items()},
        "by_signal_combo": {k: _finalize_bucket(v, False) for k, v in by_combo.items()},
        "by_strategy": {k: _finalize_bucket(v, True) for k, v in by_strategy.items()},
        "by_strategy_x_regime": {k: _finalize_bucket(v, True) for k, v in by_strategy_x_regime.items()},
    }

    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out


def load() -> Optional[dict]:
    """读 calibration.json。文件不存在返回 None（不自动 compute，避免 UI 路径产生意外延迟）。"""
    p = _path()
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def calibrate_confidence(raw_confidence: int,
                         verdict: str,
                         n_min: int = 5,
                         calibration: Optional[dict] = None) -> tuple[float, str]:
    """根据历史校准数据修正一次 AI confidence。

    返回 (calibrated_score, explanation)。explanation 永远非空（用于注入 prompt 或日志）。
    样本 < n_min 时返回原始置信度 + "样本不足"提示。
    """
    if calibration is None:
        calibration = load()
    if not calibration:
        return float(raw_confidence), "无校准数据，使用原始置信度"

    try:
        conf_int = int(raw_confidence)
    except (TypeError, ValueError):
        return float(raw_confidence or 0), "raw_confidence 解析失败"

    bucket_key = f"{verdict}@{_bucket_for(conf_int)}"
    b = (calibration.get("by_bucket") or {}).get(bucket_key)
    if not b:
        return float(conf_int), f"该桶 {bucket_key} 历史无样本"
    n = b.get("n", 0)
    if n < n_min:
        return float(conf_int), f"样本不足（{bucket_key} n={n} < {n_min}）"

    win_rate = float(b.get("win_rate") or 0.0)
    avg_pnl = float(b.get("avg_pnl_pct") or 0.0)
    cal = round(win_rate * 10, 1)
    return cal, (
        f"{bucket_key} 历史 {n} 笔，胜率 {win_rate * 100:.0f}%，"
        f"平均 P&L {avg_pnl * 100:+.2f}% → 校准 {cal}/10（原始 {conf_int}）"
    )


def inject_into(base_prompt: str, n_min: int = 3) -> str:
    """把 calibration 块注入到 base_prompt 末尾。
    如果 base_prompt 含 `{{CALIBRATION}}` 占位符则替换；否则追加。
    无校准数据时返回原文不变。
    """
    block = format_for_prompt(n_min=n_min)
    if not block:
        return base_prompt.replace("{{CALIBRATION}}", "")
    placeholder = "{{CALIBRATION}}"
    if placeholder in base_prompt:
        return base_prompt.replace(placeholder, block)
    return f"{base_prompt.rstrip()}\n\n{block}\n"


def format_for_prompt(n_min: int = 3, calibration: Optional[dict] = None) -> str:
    """把 calibration 数据格式化为 markdown，可直接拼到 system prompt。
    n_min: 桶内样本数门槛（小样本不进表，避免 1-2 笔噪音误导 AI）。
    样本不足或文件不存在时返回 ""。
    """
    if calibration is None:
        calibration = load()
    if not calibration:
        return ""
    eligible = int(calibration.get("eligible_for_calibration") or 0)
    if eligible == 0:
        return ""

    lines = [f"## 你的历史校准（基于 {eligible} 笔已平仓交易）"]

    buckets = calibration.get("by_bucket") or {}
    rows = [(k, v) for k, v in buckets.items() if (v.get("n") or 0) >= n_min]
    rows.sort(key=lambda kv: -(kv[1].get("n") or 0))

    if rows:
        lines.append("\n### verdict × confidence 桶")
        lines.append("| 桶 | n | 胜率 | 平均 P&L | 最差单笔 |")
        lines.append("|---|---|---|---|---|")
        for k, b in rows:
            worst = b.get("worst_pnl_pct")
            worst_str = f"{worst * 100:+.2f}%" if worst is not None else "-"
            lines.append(
                f"| {k} | {b['n']} | {b['win_rate'] * 100:.0f}% | "
                f"{b['avg_pnl_pct'] * 100:+.2f}% | {worst_str} |"
            )

    combos = calibration.get("by_signal_combo") or {}
    combo_rows = [(k, v) for k, v in combos.items() if (v.get("n") or 0) >= n_min]
    combo_rows.sort(key=lambda kv: -(kv[1].get("n") or 0))

    if combo_rows:
        lines.append("\n### 信号组合表现（screener 入仓的笔）")
        lines.append("| 组合 | n | 胜率 | 平均 P&L |")
        lines.append("|---|---|---|---|")
        for k, b in combo_rows:
            lines.append(
                f"| {k} | {b['n']} | {b['win_rate'] * 100:.0f}% | "
                f"{b['avg_pnl_pct'] * 100:+.2f}% |"
            )

    strategies = calibration.get("by_strategy") or {}
    strat_rows = [(k, v) for k, v in strategies.items() if (v.get("n") or 0) >= n_min]
    strat_rows.sort(key=lambda kv: -(kv[1].get("n") or 0))

    if strat_rows:
        lines.append("\n### 按选股策略表现")
        lines.append("| 策略 | n | 胜率 | 平均 P&L | 最差单笔 |")
        lines.append("|---|---|---|---|---|")
        for k, b in strat_rows:
            worst = b.get("worst_pnl_pct")
            worst_str = f"{worst * 100:+.2f}%" if worst is not None else "-"
            lines.append(
                f"| {k} | {b['n']} | {b['win_rate'] * 100:.0f}% | "
                f"{b['avg_pnl_pct'] * 100:+.2f}% | {worst_str} |"
            )

    sxr = calibration.get("by_strategy_x_regime") or {}
    sxr_rows = [(k, v) for k, v in sxr.items() if (v.get("n") or 0) >= n_min]
    sxr_rows.sort(key=lambda kv: -(kv[1].get("n") or 0))

    if sxr_rows:
        lines.append("\n### 策略 × regime 表现（重要：同策略在不同 regime 下胜率可能差很多）")
        lines.append("| 策略@regime | n | 胜率 | 平均 P&L | 最差单笔 |")
        lines.append("|---|---|---|---|---|")
        for k, b in sxr_rows:
            worst = b.get("worst_pnl_pct")
            worst_str = f"{worst * 100:+.2f}%" if worst is not None else "-"
            lines.append(
                f"| {k} | {b['n']} | {b['win_rate'] * 100:.0f}% | "
                f"{b['avg_pnl_pct'] * 100:+.2f}% | {worst_str} |"
            )

    if not rows and not combo_rows and not strat_rows and not sxr_rows:
        # 全是小样本，仅显示总数
        lines.append(f"\n（所有桶样本数 < {n_min}，暂不展示明细。继续积累交易后会自动出现。）")

    lines.append(
        "\n**怎么用这张表**：confidence 8 隐含约 80% 胜率，confidence 6 隐含约 60%。"
        "若历史某桶实际胜率显著低于隐含值（比如「看多@7-10」实际只有 38%），说明你**系统性高估**该类 setup —— "
        "本次判断同类 setup 时把 confidence 主动下调 1-2 档；反之若历史胜率高于隐含值，可适度上调。"
    )

    return "\n".join(lines)
