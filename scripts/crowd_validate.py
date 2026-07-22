"""Crowd Behavior 先验验证脚本 (Step 0.5 / T1)。

2 周 go/no-go: 验 crowd 信号 (千股千评 综合得分/关注指数/机构参与度/市场参与意愿)
极端值是否预测次日反转, 命中率 vs 朴素基准 (动量/基率/随机)。决定是否建 T2-T8 股吧爬虫。

用法 (repo root 下跑):
  python scripts/crowd_validate.py collect    # 每个交易日盘后跑: 采快照 + pre-log 预测
  python scripts/crowd_validate.py show       # 看最新快照: 信号TOP/desire/预测/进度 (可带日期)
  python scripts/crowd_validate.py status     # 看已采集天数 + 预测数 (一行/日)
  python scripts/crowd_validate.py score      # 2 周后跑: 命中率 vs 基准 -> GO/NO-GO

pre-log 完整性: RULES 写死在本文件 (先于任何 outcome, git 可证); 快照 append-only;
collect 时即落 predictions/{date}.json manifest (规则+信号值+预测方向), score 只读不算新规则。
"""
import json
import os
import sys
from pathlib import Path

# scripts/ 不在包内, 把 repo root 上 sys.path 让 from apex import ... 可用。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apex import crowd_qqp

# ---- 预测规则 (写死, 先于任何 outcome; 改规则须 bump 版本号并重跑) ----
# contrarian 假设: 信号极端 -> 次日反转 (高->跌, 低->涨)。
# signal: market 面板字段 (composite_score/focus_index/inst_participation) 或 per-stock desire。
# side: 'high' -> predict_dir -1 (反转跌); 'low' -> predict_dir +1 (反转涨)。
# threshold: 校准自 2026-07-17 信号分布 (pre-outcome, 仅看 predictor 不看结果):
#   composite_score max=78/p99=72 -> 70 fire top~1%; focus p95=87 -> 85 fire ~8% (大 N);
#   inst p99=0.51 -> 0.6 fire ~0.3%; desire(pop n=15) max=68/min=36 -> 60/40 fire 1-3 票/日。
RULES = [
    {"name": "score_high_rev",  "signal": "composite_score", "side": "high", "threshold": 70.0, "predict_dir": -1},
    {"name": "focus_high_rev",  "signal": "focus_index",     "side": "high", "threshold": 85.0, "predict_dir": -1},
    {"name": "inst_high_rev",   "signal": "inst_participation", "side": "high", "threshold": 0.6, "predict_dir": -1},
    {"name": "desire_high_rev", "signal": "desire",          "side": "high", "threshold": 60.0, "predict_dir": -1},
    {"name": "desire_low_rev",  "signal": "desire",          "side": "low",  "threshold": 40.0, "predict_dir": +1},
]

MIN_N = 20        # 低于此样本量不做 GO 判定 (统计噪声)
WIN_MARGIN = 0.05  # 命中率须超所有基准 +5pp 才算赢


def _latest_desire(rows: list) -> dict | None:
    """desire 历史取最新日 (按 date 排序, 容错 error 行)。"""
    valid = [r for r in (rows or []) if isinstance(r, dict) and "error" not in r and r.get("date")]
    if not valid:
        return None
    return max(valid, key=lambda r: r["date"])


def _fires(value, side: str, threshold: float) -> bool:
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return v >= threshold if side == "high" else v <= threshold


def write_predictions(snap: dict) -> Path:
    """从当日快照按 RULES 算 pre-log 预测, 落 predictions/{trade_date}.json。

    desire 强制 latest.date == trade_date 才触发 (防东财滞后污染 1 日对齐)。
    """
    trade_date = snap["trade_date"]
    market_by_code = {r["ts_code"]: r for r in snap.get("market", []) if r.get("ts_code")}
    desire_map = snap.get("desire", {}) or {}
    preds = []

    # 1) market 面板信号: 全市场扫 (大样本)
    for rule in RULES:
        if rule["signal"] == "desire":
            continue
        for tc, rec in market_by_code.items():
            val = rec.get(rule["signal"])
            if _fires(val, rule["side"], rule["threshold"]):
                preds.append({
                    "rule": rule["name"], "ts_code": tc,
                    "signal": rule["signal"], "signal_value": val,
                    "predict_dir": rule["predict_dir"], "trade_date": trade_date,
                })

    # 2) desire 信号: 仅持仓集, 强制日期对齐
    for rule in RULES:
        if rule["signal"] != "desire":
            continue
        for tc, rows in desire_map.items():
            latest = _latest_desire(rows)
            if not latest or latest.get("date") != trade_date:
                continue
            val = latest.get("desire")
            if _fires(val, rule["side"], rule["threshold"]):
                preds.append({
                    "rule": rule["name"], "ts_code": tc,
                    "signal": "desire", "signal_value": val,
                    "predict_dir": rule["predict_dir"], "trade_date": trade_date,
                })

    out_dir = crowd_qqp._cache_dir() / "qqp" / "predictions"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{trade_date}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"trade_date": trade_date, "n": len(preds), "predictions": preds},
                  f, ensure_ascii=False, default=str)
    return p


def _dir_of_pct(pct) -> int | None:
    """涨跌幅 -> 方向 (+1/-1); 0 平盘 / None -> None (不计)。"""
    if pct is None:
        return None
    try:
        f = float(pct)
    except (TypeError, ValueError):
        return None
    if f > 0:
        return 1
    if f < 0:
        return -1
    return None


def score() -> int:
    snaps = crowd_qqp.list_snapshots()
    if len(snaps) < 2:
        print(f"仅 {len(snaps)} 个快照, 至少需 2 个相邻交易日才能 score。先跑 collect 攒 2 周。")
        return 1
    print(f"已采集 {len(snaps)} 个交易日: {snaps[0]} .. {snaps[-1]}")

    loaded = {d: crowd_qqp.load_snapshot(d) for d in snaps}
    pred_dir = crowd_qqp._cache_dir() / "qqp" / "predictions"

    # 每条预测 -> 找 next 快照同 ts_code 的 pct_chg (次日实际方向)。
    # 基准: continuation (动量, 预测=当日方向) / base_rate (fired 集多数方向) / random 0.5。
    per_rule = {r["name"]: {
        "hits": 0, "misses": 0,  # signal 命中
        "cont_hits": 0, "cont_misses": 0,  # 动量基准 (同 stock-day)
        "up": 0, "down": 0, "skipped": 0,  # fired 集方向分布 + 无 outcome 跳过
    } for r in RULES}

    for i, d in enumerate(snaps[:-1]):
        snap = loaded[d]
        next_snap = loaded[snaps[i + 1]]
        pf = pred_dir / f"{d}.json"
        if not pf.exists():
            continue
        preds = json.load(open(pf, encoding="utf-8")).get("predictions", [])
        next_market = {r["ts_code"]: r for r in next_snap.get("market", []) if r.get("ts_code")}
        cur_market = {r["ts_code"]: r for r in snap.get("market", []) if r.get("ts_code")}

        for pr in preds:
            s = per_rule[pr["rule"]]
            tc = pr["ts_code"]
            nxt = next_market.get(tc)
            actual_dir = _dir_of_pct(nxt.get("pct_chg") if nxt else None)
            if actual_dir is None:
                s["skipped"] += 1
                continue
            # signal 命中
            if pr["predict_dir"] == actual_dir:
                s["hits"] += 1
            else:
                s["misses"] += 1
            # 动量基准: 预测 = 当日方向 (同 stock-day, apples-to-apples)
            cur_dir = _dir_of_pct(cur_market.get(tc, {}).get("pct_chg"))
            if cur_dir is not None:
                if cur_dir == actual_dir:
                    s["cont_hits"] += 1
                else:
                    s["cont_misses"] += 1
            if actual_dir > 0:
                s["up"] += 1
            else:
                s["down"] += 1

    print("\n=== Contrarian 命中率 vs 基准 (仅 fired stock-days) ===")
    print(f"{'rule':<20} {'N':>5} {'hit%':>7} {'cont%':>7} {'base%':>7} {'rand':>6} {'verdict':<10}")
    any_go = False
    for r in RULES:
        s = per_rule[r["name"]]
        n = s["hits"] + s["misses"]
        hit = s["hits"] / n if n else 0.0
        cont_n = s["cont_hits"] + s["cont_misses"]
        cont = s["cont_hits"] / cont_n if cont_n else 0.0
        base = max(s["up"], s["down"]) / n if n else 0.0
        if n < MIN_N:
            verdict = "样本不足"
        elif hit > cont + WIN_MARGIN and hit > base + WIN_MARGIN and hit > 0.5 + WIN_MARGIN:
            verdict = "GO"
            any_go = True
        else:
            verdict = "no-edge"
        print(f"{r['name']:<20} {n:>5} {hit*100:>6.1f}% {cont*100:>6.1f}% "
              f"{base*100:>6.1f}% {50.0:>5.0f}% {verdict:<10}")

    print(f"\n判定阈值: N>={MIN_N}, 赢须超 (cont / base / 0.5) 全部 +{int(WIN_MARGIN*100)}pp")
    print("\n=== GO/NO-GO ===")
    if any_go:
        print("GO: 至少一个 crowd 信号有反转预测力 -> contrarian 假设活, 建 T2-T8 股吧 text 爬虫。")
    else:
        print("NO-GO: 无 crowd 信号显著超基准 -> contrarian 假设死, 省掉股吧爬虫 + LLM 分类 (最大工程成本)。")
    return 0


def status() -> int:
    snaps = crowd_qqp.list_snapshots()
    print(f"已采集 {len(snaps)} 个交易日:")
    pred_dir = crowd_qqp._cache_dir() / "qqp" / "predictions"
    for d in snaps:
        snap = crowd_qqp.load_snapshot(d)
        n_mkt = snap.get("n_market", "?") if snap else "?"
        n_pop = snap.get("n_population", "?") if snap else "?"
        pf = pred_dir / f"{d}.json"
        n_pred = json.load(open(pf, encoding="utf-8")).get("n", "?") if pf.exists() else "-"
        print(f"  {d}: market={n_mkt} population={n_pop} predictions={n_pred}")
    return 0


def _fmt_val(v, prec=2):
    if v is None:
        return "-"
    try:
        return f"{float(v):.{prec}f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_pct(v):
    if v is None:
        return "-"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def show(date: str | None = None) -> int:
    """终端看最新 (或指定) 快照: 信号 TOP / 持仓集 desire / 当日 pre-log 预测 / 进度。"""
    snaps = crowd_qqp.list_snapshots()
    if not snaps:
        print("无快照。先跑 collect。")
        return 1
    d = date or snaps[-1]
    snap = crowd_qqp.load_snapshot(d)
    if not snap:
        print(f"无 {d} 快照。已有: {snaps}")
        return 1
    market = snap.get("market", [])
    by_code = {r["ts_code"]: r for r in market if r.get("ts_code")}
    desire_map = snap.get("desire", {}) or {}

    print(f"=== 千股千评快照 {snap['trade_date']} (采集于 {snap.get('collected_at','?')}) ===")
    print(f"市场 {snap.get('n_market','?')} 票 | 持仓集 desire {snap.get('n_population','?')} 票 | "
          f"已采集 {len(snaps)} 个交易日 (score 需 ≥2, 目标 ~10)\n")

    # 信号 TOP (extreme high = contrarian 反转跌候选)
    for sig, label, prec in [("composite_score", "综合得分", 1),
                             ("focus_index", "关注指数", 1),
                             ("inst_participation", "机构参与度", 3)]:
        rows = [(r["ts_code"], r.get("name", ""), r.get(sig))
                for r in market if r.get(sig) is not None]
        rows.sort(key=lambda x: float(x[2]), reverse=True)
        print(f"-- {label} TOP 8 (反转跌候选) --")
        for tc, name, v in rows[:8]:
            cur = by_code.get(tc, {})
            print(f"  {tc:<11} {name:<10} {label}={_fmt_val(v, prec):>7}  当日{_fmt_pct(cur.get('pct_chg'))}")
        print()

    # desire (持仓集, 最新日)
    print("-- 市场参与意愿 (持仓集, 最新日) --")
    drows = []
    for tc, hist in desire_map.items():
        latest = _latest_desire(hist)
        if not latest:
            continue
        drows.append((tc, by_code.get(tc, {}).get("name", ""),
                      latest.get("desire"), latest.get("desire_chg")))
    drows.sort(key=lambda x: float(x[2]) if x[2] is not None else -1, reverse=True)
    for tc, name, des, chg in drows:
        flag = ""
        if des is not None:
            dv = float(des)
            if dv >= 60:
                flag = "  -> 反转跌候选"
            elif dv <= 40:
                flag = "  -> 反转涨候选"
        print(f"  {tc:<11} {name:<10} desire={_fmt_val(des, 1):>6}  chg={_fmt_val(chg, 2):>7}{flag}")
    print()

    # 当日 pre-log 预测
    pf = crowd_qqp._cache_dir() / "qqp" / "predictions" / f"{d}.json"
    if pf.exists():
        preds = json.load(open(pf, encoding="utf-8")).get("predictions", [])
        from collections import Counter
        cnt = Counter(p["rule"] for p in preds)
        print(f"-- 当日 pre-log 预测 ({len(preds)} 条) --")
        for rule, n in cnt.most_common():
            r = next(x for x in RULES if x["name"] == rule)
            arrow = "涨" if r["predict_dir"] > 0 else "跌"
            print(f"  {rule:<20} {n:>4} 条  ({r['signal']} {r['side']}>{r['threshold']} -> 预测{arrow})")
        print("  样例:")
        for p in preds[:8]:
            name = by_code.get(p["ts_code"], {}).get("name", "")
            arrow = "涨" if p["predict_dir"] > 0 else "跌"
            print(f"    {p['ts_code']:<11} {name:<10} {p['rule']:<20} val={_fmt_val(p['signal_value'], 2):>7} -> 预测{arrow}")
    print()
    if len(snaps) >= 2:
        print(f"已有 {len(snaps)} 个快照, 可跑 `score` 看当前命中率 (2 周后结论才稳)。")
    else:
        print(f"仅 {len(snaps)} 个快照, 攒到 ≥2 才能跑 `score`。每个交易日盘后跑一次 collect。")
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = sys.argv[1]
    if cmd == "collect":
        snap = crowd_qqp.collect()
        p = write_predictions(snap)
        print(f"[collect] trade_date={snap['trade_date']} market={snap['n_market']} "
              f"population={snap['n_population']}")
        print(f"  snapshot  -> {crowd_qqp._snapshots_dir() / (snap['trade_date'] + '.json')}")
        print(f"  predictions ({p.name})")
        print(f"  看数据: python scripts/crowd_validate.py show")
        return 0
    if cmd == "show":
        return show(sys.argv[2] if len(sys.argv) > 2 else None)
    if cmd == "score":
        return score()
    if cmd == "status":
        return status()
    print(f"未知命令: {cmd}\n")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
