"""Daily briefing: scan watchlist, re-analyze triggered stocks, print report."""
from datetime import date

from apex import config, data, watchlist, analyze, journal


def run() -> None:
    wl = watchlist.load()
    all_codes = (
        [p["ts_code"] for p in wl["active_positions"]] +
        [c["ts_code"] for c in wl["candidates"]]
    )

    if not all_codes:
        print("⚠ Watchlist 为空。用 `python main.py watchlist add` 添加持仓或候选股。")
        return

    print(f"\n获取实时价格...")
    realtime = data.get_realtime_price(all_codes)
    daily = data.get_latest_price(all_codes)
    # 盘中实时优先；停牌/失败时回退到日线收盘
    prices = {c: (realtime.get(c) if realtime.get(c) is not None else daily.get(c))
              for c in all_codes}

    triggered_pos, triggered_cand = watchlist.get_triggered(prices)

    today = date.today().isoformat()
    print(f"\n{'='*55}")
    print(f"  📋 apex 每日晨报 — {today}")
    print(f"{'='*55}\n")

    # ── Triggered positions ──
    if triggered_pos:
        print(f"🚨 持仓触发（需要决策）\n")
        for item in triggered_pos:
            code = item["ts_code"]
            cur = item.get("current_price", "?")
            entry_p = item.get("entry_price", "?")
            stop = item.get("stop_loss", "?")
            target = item.get("target", "?")
            pnl = ((cur - entry_p) / entry_p * 100) if isinstance(cur, float) and isinstance(entry_p, float) else None
            pnl_str = f"  浮动盈亏: {pnl:+.1f}%" if pnl is not None else ""
            print(f"  {code} | 现价 {cur} | 进场 {entry_p} | 止损 {stop} | 目标 {target}{pnl_str}")
            print(f"  触发价 {item.get('trigger_price')} ({item.get('trigger_direction')}) — 重新分析中...")
            try:
                result = analyze.run(code, save=True)
                print(f"  → 新判断: {result['verdict']} (置信度 {result['confidence']})")
                pa = result.get("price_advice", {})
                if pa.get("stop_loss"):
                    print(f"     建议止损: {pa['stop_loss']}  目标: {pa.get('target', '?')}")
            except Exception as e:
                print(f"  ⚠ 分析失败: {e}")
            print()
    else:
        print("✅ 持仓：无触发\n")

    # ── Position status ──
    wl_fresh = watchlist.load()
    active = wl_fresh["active_positions"]
    if active:
        print(f"📊 持仓状态\n")
        for item in active:
            code = item["ts_code"]
            cur = prices.get(code, "?")
            entry_p = item.get("entry_price", "?")
            stop = item.get("stop_loss", "?")
            target = item.get("target", "?")
            pnl = ((cur - entry_p) / entry_p * 100) if isinstance(cur, float) and isinstance(entry_p, float) else None
            pnl_str = f"{pnl:+.1f}%" if pnl is not None else "?"
            expires = item.get("expires_at", "?")
            print(f"  {code} {item.get('name','')} | 现价 {cur} | 进场 {entry_p} | {pnl_str} | 止损 {stop} | 目标 {target} | 到期 {expires}")
        print()

    # ── Triggered candidates ──
    if triggered_cand:
        print(f"📡 候选触发（可考虑建仓）\n")
        for item in triggered_cand:
            code = item["ts_code"]
            cur = item.get("current_price", "?")
            print(f"  {code} {item.get('name','')} | 现价 {cur} | 触发价 {item.get('trigger_price')} ({item.get('trigger_direction')})")
            print(f"  备注: {item.get('note', '')} — 重新分析中...")
            try:
                result = analyze.run(code, save=True)
                print(f"  → 新判断: {result['verdict']} (置信度 {result['confidence']})")
            except Exception as e:
                print(f"  ⚠ 分析失败: {e}")
            print()

    # ── Waiting candidates ──
    waiting = [c for c in wl_fresh["candidates"] if c["ts_code"] not in {x["ts_code"] for x in triggered_cand}]
    if waiting:
        print(f"👀 候选观察（等待触发）\n")
        for item in waiting:
            code = item["ts_code"]
            cur = prices.get(code, "?")
            print(f"  {code} {item.get('name','')} | 现价 {cur} | 等待 {item.get('trigger_direction')} {item.get('trigger_price')} | 到期 {item.get('expires_at','?')}")
        print()

    print(f"{'='*55}\n")
