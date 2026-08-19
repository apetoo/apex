"""apex CLI — A 股 AI 交易闭环系统命令行入口。

Usage:
    python main.py analyze 002050.SZ
    python main.py backtest [--ts-code 002050.SZ]
    python main.py briefing
    python main.py watchlist
    python main.py screener
    python main.py realtime 002241.SZ 002050.SZ
    python main.py promote 002050.SZ --entry-price 45.0 --stop-loss 42.0 --target 50.0
"""

import os
import sys
import json
from datetime import datetime

import click

# ── 代理清理（沙箱/系统代理干扰内网 API）─────────────────────────────
def _clean_proxy():
    for k in list(os.environ):
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(k, None)


# ── analyze ────────────────────────────────────────────────────────
@click.command()
@click.argument("ts_code")
@click.option("--no-save", is_flag=True, default=False, help="不保存分析结果")
def analyze(ts_code, no_save):
    """运行 DeepSeek AI 分析单只股票"""
    _clean_proxy()
    from apex.analyze import run
    result = run(ts_code, save=not no_save)
    if result.get("analysis_status") == "insufficient_evidence":
        click.echo(f"\n{'='*50}")
        click.echo(f"  {ts_code} → 证据不足，暂不判断")
        click.echo(f"{'='*50}")
        summary = result.get("research_summary")
        if summary:
            click.echo(summary)
        unknowns = result.get("unknowns") or []
        if unknowns:
            click.echo("关键未知项：")
            for unknown in unknowns:
                click.echo(f"  - {unknown}")
        if not no_save:
            click.echo(f"\n✓ 已保存补证记录: {ts_code}")
        return
    verdict = result.get("verdict", "?")
    confidence = result.get("confidence", "?")
    analysis = result.get("analysis_text", "")
    click.echo(f"\n{'='*50}")
    click.echo(f"  {ts_code} → {verdict} (置信度 {confidence})")
    click.echo(f"{'='*50}")
    if analysis:
        click.echo(analysis[:800])
        if len(analysis) > 800:
            click.echo(f"\n...（共 {len(analysis)} 字）")
    if not no_save:
        click.echo(f"\n✓ 已保存到日志: {ts_code} → {verdict}")


# ── backtest ───────────────────────────────────────────────────────
@click.command()
@click.option("--ts-code", default=None, help="限定某只股票（默认全部）")
@click.option("--detail/--summary", default=False, help="输出明细")
def backtest(ts_code, detail):
    """回测所有看多信号的收益率"""
    _clean_proxy()
    from apex.backtest import run, print_summary
    df = run(ts_code=ts_code)
    if df.empty:
        click.echo("⚠ 没有可回测的信号")
        return
    if detail:
        click.echo(df.to_string())
    else:
        print_summary(df)


# ── briefing ───────────────────────────────────────────────────────
@click.command()
def briefing():
    """每日晨报：持仓状态 + 候选触发检查"""
    _clean_proxy()
    from apex.briefing import run as briefing_run
    briefing_run()


# ── watchlist ──────────────────────────────────────────────────────
@click.command()
@click.option("--realtime/--no-realtime", default=True, help="获取实时价格")
def watchlist(realtime):
    """查看持仓/候选/归档列表"""
    _clean_proxy()
    from apex import watchlist, data
    wl = watchlist.load()

    # 收集所有 ts_code
    all_codes = []
    for section in ("active_positions", "candidates"):
        for item in wl.get(section, []):
            if item.get("ts_code"):
                all_codes.append(item["ts_code"])

    prices = {}
    if all_codes and realtime:
        prices = data.get_realtime_price(all_codes)
        # fallback to daily close
        if not any(v is not None for v in prices.values()):
            prices = data.get_latest_price(all_codes)

    def _price_str(ts_code):
        p = prices.get(ts_code)
        return f"{p:.2f}" if p is not None else "N/A"

    click.echo(f"\n{'='*50}")
    click.echo(f"  📋 Watchlist ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    click.echo(f"{'='*50}")

    # 持仓
    positions = wl.get("active_positions", [])
    click.echo(f"\n📈 持仓 ({len(positions)})")
    for p in positions:
        entry = p.get("entry_price", "?")
        now = _price_str(p["ts_code"])
        pnl = f"({(float(now)-float(entry))/float(entry)*100:+.1f}%)" if now != "N/A" and entry != "?" else ""
        sl = p.get("stop_loss", "-")
        tp = p.get("target", "-")
        click.echo(f"  {p['ts_code']} {p.get('name','')}  进={entry} 现={now}{pnl}  止损={sl}  目标={tp}")

    # 候选
    candidates = wl.get("candidates", [])
    click.echo(f"\n⭐ 候选 ({len(candidates)})")
    for c in candidates:
        trigger = c.get("trigger_price", "-")
        now = _price_str(c["ts_code"])
        note = ""
        if now != "N/A" and trigger != "-":
            diff = (float(now) - float(trigger)) / float(trigger)
            if diff > 0:
                note = " 🟢 已触发!"
            elif diff > -0.02:
                note = " 🟡 接近触发"
        click.echo(f"  {c['ts_code']} {c.get('name','')}  触发={trigger} 现={now}{note}")

    # 归档（概要）
    archived = wl.get("archived", [])
    click.echo(f"\n📦 归档 ({len(archived)} 条)")


# ── promote ────────────────────────────────────────────────────────
@click.command()
@click.argument("ts_code")
@click.option("--entry-price", required=True, type=float, help="实际成交价")
@click.option("--stop-loss", default=None, type=float, help="止损价")
@click.option("--target", default=None, type=float, help="目标价")
def promote(ts_code, entry_price, stop_loss, target):
    """将候选股提升为持仓（实际成交后）"""
    _clean_proxy()
    from apex import watchlist
    try:
        watchlist.promote_candidate(ts_code, entry_price, stop_loss, target)
        click.echo(f"✓ {ts_code} 已从候选提升为持仓，成交价 {entry_price}")
    except Exception as e:
        click.echo(f"✗ 提升失败: {e}", err=True)


# ── realtime ───────────────────────────────────────────────────────
@click.command()
@click.argument("ts_codes", nargs=-1, required=True)
def realtime(ts_codes):
    """查询实时行情"""
    _clean_proxy()
    from apex import data
    codes = list(ts_codes)
    prices = data.get_realtime_price(codes)
    if not any(v is not None for v in prices.values()):
        prices = data.get_latest_price(codes)
        if any(v is not None for v in prices.values()):
            click.echo("（日线收盘价，非实时）")
    for code in codes:
        p = prices.get(code)
        if p is not None:
            click.echo(f"  {code}: {p:.2f}")
        else:
            click.echo(f"  {code}: N/A")


# ── screener ───────────────────────────────────────────────────────
@click.command()
@click.option("--ai/--rule-only", default=True, help="启用 AI 二次筛选")
@click.option("--top-n", default=15, type=int, help="送入 AI 的候选数")
def screener(ai, top_n):
    """运行盘后筛选器（规则 + 可选 AI）"""
    _clean_proxy()
    from apex.screener import run
    result = run(ai_enabled=ai, top_n_for_ai=top_n)
    click.echo(f"筛选完成: {len(result.get('results', []))} 条")


# ── main group ─────────────────────────────────────────────────────
class AliasedGroup(click.Group):
    def get_command(self, ctx, cmd_name):
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv
        # 支持简短别名
        aliases = {"ls": "watchlist", "bt": "backtest", "an": "analyze"}
        if cmd_name in aliases:
            return super().get_command(ctx, aliases[cmd_name])
        return None


@click.group(cls=AliasedGroup)
def cli():
    """apex — A 股 AI 交易闭环系统"""


cli.add_command(analyze)
cli.add_command(backtest)
cli.add_command(briefing)
cli.add_command(watchlist)
cli.add_command(promote)
cli.add_command(realtime)
cli.add_command(screener)

if __name__ == "__main__":
    cli()
