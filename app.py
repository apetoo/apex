import json
import sys
from pathlib import Path
from datetime import date, timedelta

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).parent))

from apex import config as cfg_module
cfg_module.load()

from apex import watchlist as wl_mod, journal, data, account as account_mod, monitor, postmortem, calibration, trace as trace_mod

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="apex 交易系统", page_icon="📈", layout="wide")
st.title("📈 apex")

# One-shot migration per session: normalize legacy ts_codes, backfill names,
# merge legacy unsuffixed journal files, dedup active_positions
if "_migrated" not in st.session_state:
    try:
        stats = wl_mod.migrate_and_backfill()
        msgs = []
        if stats.get("normalized"):
            msgs.append(f"修正 {stats['normalized']} 个代码")
        if stats.get("named"):
            msgs.append(f"补全 {stats['named']} 个名称")
        if stats.get("journal_merged"):
            msgs.append(f"合并 {stats['journal_merged']} 份历史日志")
        if stats.get("deduped"):
            msgs.append(f"去重 {stats['deduped']} 条持仓")
        if msgs:
            st.toast("✓ " + "，".join(msgs))
    except Exception:
        pass
    st.session_state["_migrated"] = True

tab_wl, tab_analyze, tab_bt, tab_screen = st.tabs(
    ["📋 Watchlist", "🔍 个股分析", "📊 回测", "📈 今日粗筛"]
)


# ── Helpers ───────────────────────────────────────────────────────────────────
VERDICT_COLOR = {
    "看多": "green", "偏多": "green", "观望偏多": "green",
    "中性": "gray", "观望": "gray",
    "观望偏空": "red", "偏空": "red", "看空": "red",
}

def verdict_badge(verdict: str) -> str:
    color = VERDICT_COLOR.get(verdict, "gray")
    return f":{color}[**{verdict}**]"


def _render_intraday_chart(ts_code: str) -> None:
    """渲染当日分时图：1分钟 close 走势线 + VWAP 均价线 + 成交量副图。

    数据来自 data.get_intraday_bars（真实价不复权）。fail-soft：无数据时静默返回。
    """
    try:
        raw = data.get_intraday_bars(ts_code)
    except Exception:
        return
    if not raw or not raw.get("bars"):
        return
    bars = raw["bars"]
    prev_close = raw.get("prev_close")
    if len(bars) < 2:
        return

    import pandas as _pd
    df = _pd.DataFrame(bars)
    # 用 "HH:MM" 字符串作 category 轴，只画有数据的分钟点，
    # 避免午休 11:30-13:00 时段被拉成平线
    df["hm"] = df["time"].str[11:16]
    df["vol_shou"] = df["vol"] / 100.0

    label = "盘中实时" if raw.get("is_intraday") else "当日"
    title = f"📈 {ts_code} {label}分时（截至 {raw.get('as_of_time','?')[11:16]}）"

    fig = go.Figure()
    # 走势线
    fig.add_trace(go.Scatter(
        x=df["hm"], y=df["close"], mode="lines", name="现价",
        line=dict(color="#e84040", width=1.5),
    ))
    # 昨收虚线
    if prev_close:
        fig.add_hline(y=prev_close, line_dash="dot", line_color="gray",
                      annotation_text=f"昨收 {prev_close}", annotation_position="top left")
    # VWAP
    df["_cum_amount"] = df["amount"].cumsum()
    df["_cum_vol"] = df["vol"].replace(0, _pd.NA).cumsum()
    df["vwap"] = (df["_cum_amount"] / df["_cum_vol"]).ffill()
    fig.add_trace(go.Scatter(
        x=df["hm"], y=df["vwap"], mode="lines", name="VWAP",
        line=dict(color="#ffa726", width=1, dash="dash"),
    ))
    fig.update_layout(
        title=title, height=380, margin=dict(l=50, r=20, t=40, b=0),
        xaxis=dict(type="category", tickangle=0, nticks=12),
        yaxis=dict(title="价格", side="right", gridcolor="#f0f0f0"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        template="plotly_white",
    )
    st.plotly_chart(fig, use_container_width=True)

    # 成交量副图
    bar_colors = ["#e84040" if c >= o else "#2ecc71" for o, c in zip(df["open"], df["close"])]
    fig_vol = go.Figure()
    fig_vol.add_trace(go.Bar(x=df["hm"], y=df["vol_shou"], name="成交量(手)",
                             marker_color=bar_colors, showlegend=False))
    fig_vol.update_layout(
        height=140, margin=dict(l=50, r=20, t=0, b=30),
        xaxis=dict(type="category", tickangle=0, nticks=12, title="时间"),
        yaxis=dict(title="量(手)", gridcolor="#f0f0f0"),
        template="plotly_white",
    )
    st.plotly_chart(fig_vol, use_container_width=True)


def _render_last_analysis(ts_code: str):
    last = journal.load_latest(ts_code)
    if not last:
        st.caption(f"暂无 {ts_code} 的分析记录")
        return
    analyzed_at = last.get("analyzed_at", "")
    ts_label = analyzed_at.replace("T", " ")[:16] if analyzed_at else last.get("date", "?")
    v = last.get("verdict", "")
    conf = last.get("confidence", "?")
    cal_conf = last.get("calibrated_confidence")
    pa = last.get("price_advice") or {}
    header = f"**{ts_label}** — {verdict_badge(v)}  置信度 {conf}/10"
    if cal_conf is not None and isinstance(cal_conf, (int, float)) and abs(cal_conf - (conf or 0)) >= 0.1:
        header += f"  →  校准 {cal_conf}/10"
    st.markdown(header)
    if pa.get("entry"):
        m1, m2, m3 = st.columns(3)
        m1.metric("建议买入", pa.get("entry", "-"))
        m2.metric("止损", pa.get("stop_loss", "-"))
        m3.metric("目标价", pa.get("target", "-"))
    text = last.get("analysis_text") or ""
    if text:
        st.markdown(text)


@st.dialog("AI 分析过程详情", width="large")
def _show_trace_dialog(ts_code: str, analyzed_at: str):
    """先用 session_state 里的内存 events（刚跑完的、含 save=False 的情况），
    再 fallback 到 trace.jsonl 文件（历史记录）。"""
    st.caption(f"{ts_code} · {analyzed_at}")
    events = None
    last = st.session_state.get("last_analysis")
    if (
        isinstance(last, dict)
        and last.get("ts_code") == ts_code
        and last.get("analyzed_at") == analyzed_at
        and last.get("_trace_events")
    ):
        events = last["_trace_events"]
    if not events:
        rec = trace_mod.load_trace(ts_code, analyzed_at)
        events = (rec or {}).get("events") or []
    if not events:
        st.warning("未找到此次分析的过程记录（可能未保存日志或在此功能上线前已分析）。")
        return
    st.caption(f"共 {len(events)} 个事件 · 默认全部折叠，点击展开看详情")
    for i, ev in enumerate(events):
        render_trace_event(ev, key_prefix=f"trace-{i}-")


_TOOL_EMOJI = {
    "get_daily_price": "📊",
    "get_fundamentals": "🏦",
    "get_stock_info": "ℹ️",
    "web_search": "🔍",
    "get_dragon_tiger_list": "🐲",
    "get_unlock_schedule": "🔓",
    "record_verdict": "📝",
}


def render_trace_event(event: dict, key_prefix: str = "") -> None:
    """Render one trace event as a folded st.expander. Default-collapsed.

    `key_prefix` ensures expander keys are unique when rendering history alongside live runs.
    """
    t = event.get("type", "")

    if t == "context":
        name = event.get("name", "")
        content = event.get("content", "")
        title_map = {
            "history": f"📜 注入 历史分析 context ({len(content)} 字)",
            "portfolio": f"💼 注入 持仓 context ({len(content)} 字)",
            "market": f"🌐 注入 大盘/板块/资金面 context ({len(content)} 字)",
        }
        with st.expander(title_map.get(name, f"📦 context: {name}"), expanded=False):
            st.markdown(content)

    elif t == "assistant_text":
        content = event.get("content", "")
        first_line = content.strip().split("\n", 1)[0][:80]
        final_tag = " (最终)" if event.get("final") else ""
        iter_n = event.get("iteration", "?")
        with st.expander(f"💭 AI 思考 iter {iter_n}{final_tag}：{first_line}", expanded=False):
            st.markdown(content)

    elif t == "tool_call":
        name = event.get("name", "")
        args = event.get("args", {}) or {}
        emoji = _TOOL_EMOJI.get(name, "🔧")
        if name == "web_search":
            cat = args.get("category", "?")
            label = args.get("name") or args.get("industry") or args.get("query") or args.get("ts_code", "")
            title = f"{emoji} 调用 {name}[{cat}] {label}"
        else:
            label = args.get("ts_code", "") or args.get("verdict", "")
            title = f"{emoji} 调用 {name}({label})"
        with st.expander(title, expanded=False):
            st.markdown("**入参**")
            st.json(args)

    elif t == "tool_result":
        name = event.get("name", "")
        summary = event.get("summary", {}) or {}
        raw = event.get("raw", "")
        if "error" in summary:
            title = f"❌ 返回 {name}：{str(summary['error'])[:60]}"
        elif name == "web_search":
            title = f"✅ 返回 {name}[{summary.get('category', '?')}] 命中 {summary.get('count', 0)} 条"
        elif name == "get_daily_price":
            title = f"✅ 返回 {name}：{summary.get('bars', '?')} 根K线，最新收盘 {summary.get('close', '?')}"
        elif name == "get_fundamentals":
            pe = summary.get('pe', '-')
            pb = summary.get('pb', '-')
            nq = summary.get('fin_quarters', 0)
            roe = summary.get('latest_roe')
            flags = summary.get('flags', [])
            parts = [f"PE={pe}", f"PB={pb}"]
            if nq:
                parts.append(f"{nq}季财务")
            if roe is not None:
                parts.append(f"ROE={roe:.1f}%")
            if flags:
                parts.append(f"⚠{len(flags)}条风险")
            title = f"✅ 返回 {name}：" + " ".join(parts)
        elif name == "get_stock_info":
            title = f"✅ 返回 {name}：{summary.get('name', '?')} / {summary.get('industry', '?')}"
        elif name == "get_dragon_tiger_list":
            title = f"✅ 返回 {name}：近 {summary.get('window_days', '?')} 天上榜 {summary.get('list_count', 0)} 次"
        elif name == "get_unlock_schedule":
            title = (
                f"✅ 返回 {name}：未来 {summary.get('future_count', 0)} 次 / "
                f"历史 {summary.get('history_count', 0)} 次"
            )
        else:
            title = f"✅ 返回 {name}"
        with st.expander(title, expanded=False):
            st.markdown("**结果摘要**")
            st.json(summary)
            if raw:
                st.markdown("**原始返回 JSON**")
                raw_str = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, indent=2)
                st.code(raw_str[:20000], language="json")
                if len(raw_str) > 20000:
                    st.caption(f"（已截断显示前 20000 字符，原始长度 {len(raw_str)}）")

    elif t == "verdict_rejected":
        missing = event.get("missing", [])
        with st.expander(f"⛔ 记录结论被拒：缺类别 {missing}", expanded=False):
            st.markdown(f"- 已完成类别：{event.get('performed', [])}")
            st.markdown(f"- 缺失类别：{missing}")

    elif t == "verdict_recorded":
        with st.expander(
            f"📝 记录结论：{event.get('verdict', '?')}（置信度 {event.get('confidence', '?')}/10）",
            expanded=False,
        ):
            st.json({k: v for k, v in event.items() if k != "type"})

    else:
        st.caption(f"❓ 未知事件 type={t}：{event}")


_DIR_MAP = {"向下跌破": "below", "向上突破": "above"}


@st.dialog("添加股票", width="large")
def _show_add_dialog():
    acc_dlg = account_mod.load()
    add_type = st.radio("类型", ["持仓", "候选"], horizontal=True, key="dlg_add_type")
    ts_code_input = st.text_input("股票代码", placeholder="002050.SZ", key="dlg_add_ts")
    name_input = st.text_input("名称（可选）", key="dlg_add_name")

    if add_type == "持仓":
        c1, c2 = st.columns(2)
        entry_input = c1.number_input("进场价", min_value=0.0, step=0.1, key="dlg_entry")
        stop_input = c2.number_input("止损价", min_value=0.0, step=0.1, key="dlg_stop")
        c3, c4 = st.columns(2)
        target_input = c3.number_input("目标价", min_value=0.0, step=0.1, key="dlg_target")
        trigger_input = c4.number_input("触发重分析价", min_value=0.0, step=0.1, key="dlg_trigger")
        direction_input = st.selectbox("触发重分析方向", ["向下跌破", "向上突破"], key="dlg_direction")
        expires_input = st.slider("有效天数", 3, 30, 10, key="dlg_expires")

        sizing = account_mod.compute_position_size(entry_input, stop_input, account=acc_dlg)
        if sizing["ok"]:
            st.info(
                f"💡 建议手数 **{sizing['shares']}** 股 ｜ "
                f"占资金 {sizing['capital_required']:,.0f} ｜ "
                f"风险 {sizing['risk_amount']:,.0f}（{sizing['risk_pct_actual']}%）"
            )
        for w in sizing["warnings"]:
            st.caption(f"⚠ {w}")

        shares_input = st.number_input(
            "成交手数（0 = 不记录仓位）",
            min_value=0, step=100,
            value=sizing["shares"] if sizing["ok"] else 0,
            key="dlg_shares",
        )

        if st.button("添加持仓", type="primary", key="dlg_pos_btn") and ts_code_input:
            code_norm = data.normalize_ts_code(ts_code_input)
            name_resolved = name_input
            if not name_resolved:
                try:
                    info_list = json.loads(data.get_stock_info(ts_code=code_norm))
                    if isinstance(info_list, list) and info_list:
                        name_resolved = info_list[0].get("name", "")
                except Exception:
                    pass
            actual_risk = (
                shares_input * abs(entry_input - stop_input)
                if shares_input and entry_input and stop_input else None
            )
            pos_args = dict(
                ts_code=code_norm, name=name_resolved,
                entry_price=entry_input, stop_loss=stop_input,
                target=target_input,
                trigger_price=trigger_input or None,
                trigger_direction=_DIR_MAP[direction_input],
                expires_days=expires_input,
                position_size_shares=shares_input or None,
                risk_amount=actual_risk,
            )
            try:
                wl_mod.add_position(**pos_args)
                st.session_state.pop("prices", None)
                st.rerun()
            except wl_mod.DuplicatePositionError as e:
                st.session_state["_dup_pos"] = {
                    "ts_code": code_norm,
                    "existing": e.existing,
                    "args": pos_args,
                }
                st.rerun()
    else:
        c1, c2 = st.columns(2)
        trigger_input = c1.number_input("触发价", min_value=0.0, step=0.1, key="dlg_cand_trigger")
        direction_input = c2.selectbox("触发方向", ["向上突破", "向下跌破"], key="dlg_cand_dir")
        note_input = st.text_input("备注", key="dlg_cand_note")
        expires_input = st.slider("有效天数", 3, 14, 7, key="dlg_cand_expires")

        if st.button("添加候选", type="primary", key="dlg_cand_btn") and ts_code_input:
            code_norm = data.normalize_ts_code(ts_code_input)
            name_resolved = name_input
            if not name_resolved:
                try:
                    info_list = json.loads(data.get_stock_info(ts_code=code_norm))
                    if isinstance(info_list, list) and info_list:
                        name_resolved = info_list[0].get("name", "")
                except Exception:
                    pass
            wl_mod.add_candidate(
                code_norm, name_resolved, trigger_input,
                _DIR_MAP[direction_input], note_input, expires_input,
            )
            st.session_state.pop("prices", None)
            st.rerun()


# ── Tab 1: Watchlist ──────────────────────────────────────────────────────────
with tab_wl:
    col_left, col_right = st.columns([5, 1])

    with col_left:
        # ── 账户管理 ────────────────────────────────────────────────────────
        acc = account_mod.load()
        with st.expander(
            f"💼 账户：总资金 {acc['total_capital']:,.0f} ｜ 单笔风险 {acc['risk_per_trade_pct']}% "
            f"｜ 总风险上限 {acc['max_total_risk_pct']}%",
            expanded=False,
        ):
            ac1, ac2, ac3 = st.columns(3)
            new_total = ac1.number_input(
                "总资金（元）", min_value=0.0, step=10000.0,
                value=float(acc["total_capital"]), key="acc_total",
            )
            new_risk = ac2.number_input(
                "单笔风险 %", min_value=0.1, max_value=10.0, step=0.1,
                value=float(acc["risk_per_trade_pct"]), key="acc_risk",
            )
            new_max = ac3.number_input(
                "总风险上限 %", min_value=1.0, max_value=50.0, step=1.0,
                value=float(acc["max_total_risk_pct"]), key="acc_max",
            )
            if st.button("保存账户配置", key="acc_save"):
                account_mod.update_capital(
                    total_capital=new_total,
                    risk_per_trade_pct=new_risk,
                    max_total_risk_pct=new_max,
                )
                st.success("✓ 已保存")
                st.rerun()

        st.subheader("持仓 & 候选")

        from datetime import datetime as _dt
        if st.button("🔄 刷新价格", key="refresh_prices"):
            st.session_state.pop("prices", None)
            st.session_state.pop("realtime_prices", None)
            st.session_state.pop("prices_ts", None)

        # Pending duplicate-position confirmation (set when add_position raises)
        if "_dup_pos" in st.session_state:
            pending = st.session_state["_dup_pos"]
            existing = pending.get("existing", {})
            new_args = pending["args"]
            st.warning(f"⚠ 持仓 {pending['ts_code']} 已存在")
            cmp_col1, cmp_col2 = st.columns(2)
            cmp_col1.caption(
                f"**现有**\n\n"
                f"进场 {existing.get('entry_price','-')}  止损 {existing.get('stop_loss','-')}  "
                f"目标 {existing.get('target','-')}  日期 {existing.get('entry_date','-')}"
            )
            cmp_col2.caption(
                f"**新值**\n\n"
                f"进场 {new_args.get('entry_price','-')}  止损 {new_args.get('stop_loss','-')}  "
                f"目标 {new_args.get('target','-')}"
            )
            b1, b2 = st.columns(2)
            if b1.button("替换（旧的归档）", key="dup_replace"):
                if pending.get("from_candidate"):
                    wl_mod.archive_entry(pending["ts_code"], "candidates", reason="promoted")
                wl_mod.replace_position(**new_args)
                del st.session_state["_dup_pos"]
                st.session_state.pop("prices", None)
                st.success(f"✓ 已更新持仓 {pending['ts_code']}")
                st.rerun()
            if b2.button("取消", key="dup_cancel"):
                del st.session_state["_dup_pos"]
                st.rerun()

        wl_data = wl_mod.load()
        all_codes = (
            [p["ts_code"] for p in wl_data["active_positions"]] +
            [c["ts_code"] for c in wl_data["candidates"]]
        )

        # 现价 = 日线收盘 (tushare pro.daily, 盘中=昨收, 不变)
        # 实时 = 盘中最新价 (akshare 东方财富, 每次刷新重取)
        prices = {}
        realtime_prices = {}
        if all_codes:
            if "prices" not in st.session_state:
                with st.spinner("获取日线收盘..."):
                    st.session_state["prices"] = data.get_latest_price(all_codes)
            if "realtime_prices" not in st.session_state:
                with st.spinner("获取实时行情..."):
                    st.session_state["realtime_prices"] = data.get_realtime_price(all_codes)
                    st.session_state["prices_ts"] = _dt.now().strftime("%H:%M:%S")
            prices = st.session_state["prices"]
            realtime_prices = st.session_state["realtime_prices"]
            ts_label = st.session_state.get("prices_ts", "")
            if ts_label:
                st.caption(f"实时行情更新于 {ts_label}")

        # ── 触发监控 banner ────────────────────────────────────────────────
        # 用已缓存的 realtime_prices 跑一次 check_once（不会重复拉行情，不会重复推送）
        if all_codes and realtime_prices:
            try:
                monitor.check_once(notify_enabled=False, prices=realtime_prices)
            except Exception as _e:
                st.caption(f"⚠ 触发检测失败: {_e}")
        unread_triggers = monitor.load_recent_triggers(hours=24)
        if unread_triggers:
            st.error(f"🚨 {len(unread_triggers)} 个未处理触发")
            for i, t in enumerate(unread_triggers):
                sig = f"{t['ts_code']}|{t['trigger_type']}|{t['trigger_price']}"
                label = {
                    "stop_loss": "🛑 止损触发",
                    "target": "🎯 目标触发",
                    "candidate": "⚡️ 候选触发",
                }.get(t["trigger_type"], "🔔 触发")
                with st.container(border=True):
                    head = (
                        f"**{label}** ｜ {t['ts_code']} {t.get('name','')} ｜ "
                        f"触发位 **{t['trigger_price']}** ｜ 现价 **{t['current_price']}** "
                        f"｜ 检测于 {t.get('detected_at','')[:19].replace('T',' ')}"
                    )
                    st.markdown(head)

                    if t["trigger_type"] in ("stop_loss", "target"):
                        # active position trigger: 内联平仓 mini-form
                        reason = "stop_hit" if t["trigger_type"] == "stop_loss" else "target_hit"
                        cols = st.columns([2, 3, 1, 1])
                        exit_price = cols[0].number_input(
                            "成交价", min_value=0.0, step=0.01,
                            value=float(t["current_price"]),
                            key=f"trig_exit_{i}",
                        )
                        notes = cols[1].text_input(
                            "备注", key=f"trig_notes_{i}",
                            placeholder="如：跳空止损 / 主动止盈",
                        )
                        if cols[2].button("✓ 平仓", key=f"trig_close_{i}", type="primary"):
                            try:
                                rec = wl_mod.close_position(
                                    ts_code=t["ts_code"],
                                    exit_price=float(exit_price),
                                    exit_reason=reason,
                                    user_notes=notes or "",
                                )
                                monitor.ack_trigger(sig)
                                pnl_pct = (rec["close"].get("realized_pnl_pct") or 0) * 100
                                with st.spinner("AI 自动复盘中…"):
                                    diag = postmortem.run_and_patch(rec)
                                try:
                                    calibration.compute()
                                except Exception as _ce:
                                    st.caption(f"⚠ 校准重算失败: {_ce}")
                                st.session_state.pop("prices", None)
                                st.session_state.pop("realtime_prices", None)
                                if diag:
                                    st.success(
                                        f"✓ 已平仓 {t['ts_code']} ｜ P&L {pnl_pct:+.2f}% ｜ "
                                        f"复盘已保存（lesson: {diag.get('lesson','')[:60]}…）"
                                    )
                                else:
                                    st.warning(
                                        f"✓ 已平仓 {t['ts_code']} ｜ P&L {pnl_pct:+.2f}%；"
                                        "AI 复盘失败，可在「已平仓」列表中手动重跑"
                                    )
                                st.rerun()
                            except wl_mod.PositionNotFoundError:
                                # position 已不在 active（比如已经被手动平仓/归档）→ 直接标已读
                                monitor.ack_trigger(sig)
                                st.warning(f"持仓 {t['ts_code']} 已不存在，标记此触发为已读")
                                st.rerun()
                            except ValueError as _e:
                                st.error(str(_e))
                        if cols[3].button("稍后", key=f"trig_skip_{i}"):
                            monitor.ack_trigger(sig)
                            st.rerun()
                    else:
                        # candidate trigger: 提示去 promote
                        cols = st.columns([4, 1, 1])
                        cols[0].caption("去下方「晋升候选为持仓」折叠区填实际成交价完成开仓。")
                        if cols[1].button("标已读", key=f"trig_ack_{i}"):
                            monitor.ack_trigger(sig)
                            st.rerun()
                        if cols[2].button("删候选", key=f"trig_drop_{i}"):
                            wl_mod.archive_entry(t["ts_code"], "candidates", reason="manual")
                            monitor.ack_trigger(sig)
                            st.session_state.pop("prices", None)
                            st.rerun()

            if st.button("📭 全部标已读", key="trig_ack_all"):
                for t in unread_triggers:
                    monitor.ack_trigger(f"{t['ts_code']}|{t['trigger_type']}|{t['trigger_price']}")
                st.rerun()

        # Active positions table
        if wl_data["active_positions"]:
            # 总风险监控 banner
            risk_summary = account_mod.current_total_risk(wl_data["active_positions"], account=acc)
            risk_pct = risk_summary["total_risk_pct"]
            if risk_pct is not None:
                pct_str = f"{risk_pct:.2f}%"
                limit_str = f"{risk_summary['max_total_risk_pct']:.0f}%"
                if risk_summary["over_limit"]:
                    st.error(f"⚠️ 总风险 {pct_str} 已超过上限 {limit_str}（{risk_summary['total_risk_amount']:,.0f} 元）")
                elif risk_pct > risk_summary["max_total_risk_pct"] * 0.7:
                    st.warning(f"⚠️ 总风险 {pct_str} / 上限 {limit_str}（{risk_summary['total_risk_amount']:,.0f} 元）")
                else:
                    st.caption(f"📊 总风险 {pct_str} / 上限 {limit_str}（{risk_summary['total_risk_amount']:,.0f} 元，{risk_summary['position_count']} 个持仓）")
                if risk_summary["missing_size_count"]:
                    st.caption(f"ℹ️ {risk_summary['missing_size_count']} 个持仓未填手数，不计入风险监控")

            st.markdown("**📊 持仓**")
            rows = []
            capital_for_pct = acc.get("total_capital") or 0
            for p in wl_data["active_positions"]:
                cur = prices.get(p["ts_code"])
                rt = realtime_prices.get(p["ts_code"])
                ref = rt if rt is not None else cur  # 实时优先，回退昨收
                entry_p = p.get("entry_price")
                pnl = (round((ref - entry_p) / entry_p * 100, 1)
                       if ref is not None and entry_p else None)
                triggered = wl_mod.is_triggered(p, ref) if ref is not None else False
                shares = p.get("position_size_shares")
                risk_amt = p.get("risk_amount")
                risk_pct_pos = (
                    round(risk_amt / capital_for_pct * 100, 2)
                    if risk_amt and capital_for_pct > 0 else None
                )
                rows.append({
                    "代码": p["ts_code"],
                    "名称": p.get("name", ""),
                    "策略": p.get("strategy", "—"),
                    "昨收": cur,
                    "实时": rt,
                    "进场": entry_p,
                    "盈亏%": pnl,
                    "手数": shares,
                    "风险%": risk_pct_pos,
                    "止损": p.get("stop_loss"),
                    "目标": p.get("target"),
                    "触发": "🚨" if triggered else "",
                    "到期": p.get("expires_at", ""),
                })
            df_pos = pd.DataFrame(rows)
            st.dataframe(
                df_pos.style.map(
                    lambda v: "color: red" if isinstance(v, float) and v > 0
                    else ("color: green" if isinstance(v, float) and v < 0 else ""),
                    subset=["盈亏%"]
                ),
                width="stretch",
                hide_index=True,
            )
            with st.expander("📋 查看最近分析"):
                pos_codes = [""] + [f"{p['ts_code']} {p.get('name','')}" for p in wl_data["active_positions"]]
                pos_detail_sel = st.selectbox("选择持仓", pos_codes, key="pos_detail_sel")
                if pos_detail_sel:
                    _render_last_analysis(pos_detail_sel.split()[0])

            with st.expander("管理持仓"):
                pos_map = {f"{p['ts_code']} {p.get('name','')}": p for p in wl_data["active_positions"]}
                close_tab, archive_tab = st.tabs(["平仓（写入复盘库）", "仅归档（不记 P&L）"])

                with close_tab:
                    pick_close = st.selectbox(
                        "选择持仓", [""] + list(pos_map.keys()), key="close_pos_sel"
                    )
                    if pick_close:
                        pos = pos_map[pick_close]
                        rt_p = realtime_prices.get(pos["ts_code"])
                        cur_p = prices.get(pos["ts_code"])
                        ref_p = rt_p if rt_p is not None else cur_p
                        default_exit = float(ref_p) if ref_p else float(pos.get("entry_price") or 0)
                        stop_p = pos.get("stop_loss")
                        target_p = pos.get("target")

                        # 默认 reason：自动按触发位推断
                        default_reason = "manual"
                        if ref_p and stop_p and ref_p <= float(stop_p):
                            default_reason = "stop_hit"
                        elif ref_p and target_p and ref_p >= float(target_p):
                            default_reason = "target_hit"

                        cc1, cc2 = st.columns(2)
                        exit_price_input = cc1.number_input(
                            "实际成交价", min_value=0.0, step=0.01,
                            value=float(default_exit), key="close_exit_price",
                        )
                        reasons = ["stop_hit", "target_hit", "manual", "expired", "other"]
                        exit_reason_input = cc2.selectbox(
                            "平仓原因", reasons,
                            index=reasons.index(default_reason),
                            key="close_exit_reason",
                        )
                        exit_date_input = st.date_input(
                            "平仓日期", value=_dt.now().date(), key="close_exit_date",
                        )
                        notes_input = st.text_input("备注（可选）", key="close_notes")

                        # 实时盈亏预览
                        entry_p = float(pos.get("entry_price") or 0)
                        shares = int(pos.get("position_size_shares") or 0)
                        if entry_p > 0 and exit_price_input > 0:
                            pnl_pct = (exit_price_input - entry_p) / entry_p * 100
                            color = "🟢" if pnl_pct >= 0 else "🔴"
                            line = f"{color} 预计盈亏 **{pnl_pct:+.2f}%**"
                            if shares > 0:
                                pnl_amt = (exit_price_input - entry_p) * shares
                                line += f" ｜ 金额 {pnl_amt:+,.0f} 元（{shares} 股 × Δ {exit_price_input - entry_p:+.2f}）"
                            st.markdown(line)

                        if st.button("确认平仓", type="primary", key="close_pos_btn"):
                            if exit_price_input <= 0:
                                st.error("成交价必须 > 0")
                            else:
                                try:
                                    rec = wl_mod.close_position(
                                        ts_code=pos["ts_code"],
                                        exit_price=float(exit_price_input),
                                        exit_reason=exit_reason_input,
                                        exit_date=exit_date_input.isoformat(),
                                        user_notes=notes_input or "",
                                    )
                                    pnl_amt = (rec["close"].get("realized_pnl_amount") or 0)
                                    pnl_pct = (rec["close"].get("realized_pnl_pct") or 0) * 100
                                    with st.spinner("AI 自动复盘中…"):
                                        diag = postmortem.run_and_patch(rec)
                                    try:
                                        calibration.compute()
                                    except Exception as _ce:
                                        st.caption(f"⚠ 校准重算失败: {_ce}")
                                    st.session_state.pop("prices", None)
                                    st.session_state.pop("realtime_prices", None)
                                    base_msg = (
                                        f"✓ 已平仓 {pos['ts_code']} {pos.get('name','')} ｜ "
                                        f"P&L {pnl_pct:+.2f}% / {pnl_amt:+,.0f} 元"
                                    )
                                    if diag:
                                        st.success(base_msg + f" ｜ 复盘已保存")
                                    else:
                                        st.warning(base_msg + "；AI 复盘失败，可在「已平仓」列表手动重跑")
                                    st.rerun()
                                except wl_mod.PositionNotFoundError as e:
                                    st.error(str(e))
                                except ValueError as e:
                                    st.error(str(e))

                with archive_tab:
                    st.caption("⚠ 归档不写入 closed_positions.jsonl，不参与复盘和校准。仅用于'误加'或'测试持仓'清理。真实平仓请用左 tab。")
                    rm_col1, rm_col2 = st.columns([3, 1])
                    rm_target = rm_col1.selectbox(
                        "归档持仓", [""] + list(pos_map.keys()), key="rm_pos_sel",
                    )
                    if rm_col2.button("归档", key="rm_pos_btn") and rm_target:
                        code_to_remove = rm_target.split()[0]
                        if wl_mod.archive_entry(code_to_remove, "active_positions"):
                            st.session_state.pop("prices", None)
                            st.success(f"✓ 已归档 {rm_target}")
                            st.rerun()
        else:
            st.info("暂无持仓")

        # Candidates table
        if wl_data["candidates"]:
            st.markdown("**👀 候选**")
            rows = []
            for c in wl_data["candidates"]:
                cur = prices.get(c["ts_code"])
                rt = realtime_prices.get(c["ts_code"])
                ref = rt if rt is not None else cur
                triggered = wl_mod.is_triggered(c, ref) if ref is not None else False
                rows.append({
                    "代码": c["ts_code"],
                    "名称": c.get("name", ""),
                    "策略": c.get("strategy", "—"),
                    "昨收": cur,
                    "实时": rt,
                    "触发价": c.get("trigger_price"),
                    "方向": {"below": "向下跌破", "above": "向上突破"}.get(c.get("trigger_direction", ""), c.get("trigger_direction", "")),
                    "触发": "🚨" if triggered else "",
                    "备注": c.get("note", ""),
                    "到期": c.get("expires_at", ""),
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            with st.expander("📋 查看最近分析"):
                cand_codes = [""] + [f"{c['ts_code']} {c.get('name','')}" for c in wl_data["candidates"]]
                cand_detail_sel = st.selectbox("选择候选", cand_codes, key="cand_detail_sel")
                if cand_detail_sel:
                    _render_last_analysis(cand_detail_sel.split()[0])

            with st.expander("管理候选"):
                rm_col1, rm_col2 = st.columns([3, 1])
                cand_options = [f"{c['ts_code']} {c.get('name','')}" for c in wl_data["candidates"]]
                rm_target = rm_col1.selectbox("归档候选", [""] + cand_options, key="rm_cand_sel")
                if rm_col2.button("归档", key="rm_cand_btn") and rm_target:
                    code_to_remove = rm_target.split()[0]
                    if wl_mod.archive_entry(code_to_remove, "candidates"):
                        st.session_state.pop("prices", None)
                        st.success(f"✓ 已归档 {rm_target}")
                        st.rerun()

            with st.expander("晋升候选为持仓（已成交后填写实际价位）"):
                cand_map = {f"{c['ts_code']} {c.get('name','')}": c for c in wl_data["candidates"]}
                pick = st.selectbox("候选", [""] + list(cand_map.keys()), key="promote_sel")
                if pick:
                    cand = cand_map[pick]
                    default_entry = float(cand.get("trigger_price") or 0.0)
                    default_stop = float(cand.get("stop_advice") or 0.0)
                    default_target = float(cand.get("target_advice") or 0.0)
                    if cand.get("note"):
                        st.caption(f"备注: {cand['note']}")
                    pc1, pc2 = st.columns(2)
                    promote_entry = pc1.number_input("实际成交价", min_value=0.0, step=0.1,
                                                     value=default_entry, key="promote_entry")
                    promote_stop = pc2.number_input("止损价", min_value=0.0, step=0.1,
                                                    value=default_stop, key="promote_stop")
                    pc3, pc4 = st.columns(2)
                    promote_target = pc3.number_input("目标价", min_value=0.0, step=0.1,
                                                      value=default_target, key="promote_target")
                    promote_expires = pc4.slider("有效天数", 3, 30, 10, key="promote_expires")

                    promote_sizing = account_mod.compute_position_size(
                        promote_entry, promote_stop, account=acc,
                    )
                    promote_default_shares = promote_sizing["shares"] if promote_sizing["ok"] else 0
                    if promote_sizing["ok"]:
                        st.info(
                            f"💡 建议手数 **{promote_sizing['shares']}** 股 ｜ "
                            f"占资金 {promote_sizing['capital_required']:,.0f} ｜ "
                            f"风险 {promote_sizing['risk_amount']:,.0f}"
                            f"（{promote_sizing['risk_pct_actual']}%）"
                        )
                    for w in promote_sizing["warnings"]:
                        st.caption(f"⚠ {w}")

                    promote_shares = st.number_input(
                        "成交手数（0 = 不记录仓位）",
                        min_value=0, step=100,
                        value=promote_default_shares,
                        key="promote_shares",
                    )

                    if st.button("晋升为持仓", type="primary", key="promote_btn"):
                        ts = cand["ts_code"]
                        actual_risk = (
                            promote_shares * abs(promote_entry - promote_stop)
                            if promote_shares and promote_entry and promote_stop else None
                        )
                        # 反查 journal latest，把 calibrated_confidence 也带进持仓
                        cal_from_journal = None
                        try:
                            j = journal.load_latest(ts)
                            if j:
                                cal_from_journal = j.get("calibrated_confidence")
                        except Exception:
                            pass
                        promote_args = dict(
                            ts_code=ts,
                            entry_price=promote_entry,
                            stop_loss=promote_stop,
                            target=promote_target,
                            expires_days=promote_expires,
                            position_size_shares=promote_shares or None,
                            risk_amount=actual_risk,
                            calibrated_confidence=cal_from_journal,
                        )
                        try:
                            wl_mod.promote_candidate(**promote_args)
                            st.session_state.pop("prices", None)
                            st.success(f"✓ 已晋升 {ts} 为持仓")
                            st.rerun()
                        except wl_mod.DuplicatePositionError as e:
                            st.session_state["_dup_pos"] = {
                                "ts_code": ts,
                                "existing": e.existing,
                                "args": dict(
                                    ts_code=ts,
                                    name=cand.get("name", ""),
                                    entry_price=promote_entry,
                                    stop_loss=promote_stop,
                                    target=promote_target,
                                    trigger_price=None,
                                    trigger_direction="below",
                                    expires_days=promote_expires,
                                    position_size_shares=promote_shares or None,
                                    risk_amount=actual_risk,
                                ),
                                "from_candidate": True,
                            }
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))
        else:
            st.info("暂无候选")

        # ── AI 校准（自我进化的核心信号）──────────────────────────────────────
        cal_data = calibration.load()
        if cal_data and cal_data.get("eligible_for_calibration", 0) > 0:
            elig = cal_data["eligible_for_calibration"]
            n_buckets = sum(1 for v in (cal_data.get("by_bucket") or {}).values()
                            if v.get("n", 0) >= 3)
            updated = (cal_data.get("computed_at") or "")[:19].replace("T", " ")
            with st.expander(
                f"🎯 AI 校准 ｜ {elig} 笔已用 ｜ {n_buckets} 个桶达 n≥3 ｜ 更新于 {updated}"
            ):
                cb1, cb2 = st.columns([5, 1])
                cb1.caption(
                    "下方 verdict×confidence 桶展示你历史上每类判断的实际胜率。"
                    "Phase 1.6 起会把这张表注入 analyze.py / screener.py 的 system prompt，"
                    "让 AI 看到自己的成绩单后自动校准 confidence。"
                )
                if cb2.button("🔄 重算", key="cal_recompute"):
                    try:
                        calibration.compute()
                        st.success("✓ 已重算")
                        st.rerun()
                    except Exception as _e:
                        st.error(f"重算失败: {_e}")

                buckets = cal_data.get("by_bucket") or {}
                rows = []
                for k, v in sorted(buckets.items(), key=lambda kv: -(kv[1].get("n") or 0)):
                    rows.append({
                        "桶": k,
                        "n": v["n"],
                        "胜率": f"{v['win_rate'] * 100:.0f}%",
                        "平均 P&L": f"{v['avg_pnl_pct'] * 100:+.2f}%",
                        "最佳": (f"{v['best_pnl_pct'] * 100:+.2f}%"
                                 if v.get("best_pnl_pct") is not None else "-"),
                        "最差": (f"{v['worst_pnl_pct'] * 100:+.2f}%"
                                 if v.get("worst_pnl_pct") is not None else "-"),
                    })
                if rows:
                    st.markdown("**verdict × confidence 桶**")
                    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
                strategies = cal_data.get("by_strategy") or {}
                if strategies:
                    strat_rows = []
                    for k, v in sorted(strategies.items(), key=lambda kv: -(kv[1].get("n") or 0)):
                        strat_rows.append({
                            "策略": k, "n": v["n"],
                            "胜率": f"{v['win_rate'] * 100:.0f}%",
                            "平均 P&L": f"{v['avg_pnl_pct'] * 100:+.2f}%",
                            "最差": (f"{v['worst_pnl_pct'] * 100:+.2f}%"
                                     if v.get("worst_pnl_pct") is not None else "-"),
                        })
                    st.markdown("**按选股策略表现**（全样本，S2 起 AI 据此选当日策略权重）")
                    st.dataframe(pd.DataFrame(strat_rows), hide_index=True, width="stretch")

                sxr = cal_data.get("by_strategy_x_regime") or {}
                if sxr:
                    sxr_rows = []
                    for k, v in sorted(sxr.items(), key=lambda kv: -(kv[1].get("n") or 0)):
                        sxr_rows.append({
                            "策略 @ regime": k, "n": v["n"],
                            "胜率": f"{v['win_rate'] * 100:.0f}%",
                            "平均 P&L": f"{v['avg_pnl_pct'] * 100:+.2f}%",
                            "最差": (f"{v['worst_pnl_pct'] * 100:+.2f}%"
                                     if v.get("worst_pnl_pct") is not None else "-"),
                        })
                    st.markdown("**策略 × regime 表现**（S3 起 AI selector 优先看此表，n<5 回退全样本）")
                    st.dataframe(pd.DataFrame(sxr_rows), hide_index=True, width="stretch")
                    st.caption(
                        "💡 同策略在不同 regime 下胜率可能差 30%+。"
                        "比如 first_board_leader 可能在 risk_on 下 75%、neutral 下 33% —— "
                        "这就是为什么 selector 要看 regime。"
                    )

                combos = cal_data.get("by_signal_combo") or {}
                if combos:
                    combo_rows = []
                    for k, v in sorted(combos.items(), key=lambda kv: -(kv[1].get("n") or 0)):
                        combo_rows.append({
                            "信号组合": k, "n": v["n"],
                            "胜率": f"{v['win_rate'] * 100:.0f}%",
                            "平均 P&L": f"{v['avg_pnl_pct'] * 100:+.2f}%",
                        })
                    st.markdown("**信号组合（screener 入仓的笔）**")
                    st.dataframe(pd.DataFrame(combo_rows), hide_index=True, width="stretch")

        # ── 已平仓历史 ────────────────────────────────────────────────────────
        closed_recent = wl_mod.load_closed_positions(limit=50)
        if closed_recent:
            wins = sum(1 for r in closed_recent
                       if (r.get("close") or {}).get("realized_pnl_pct") is not None
                       and r["close"]["realized_pnl_pct"] > 0)
            losses = sum(1 for r in closed_recent
                         if (r.get("close") or {}).get("realized_pnl_pct") is not None
                         and r["close"]["realized_pnl_pct"] <= 0)
            total_with_pnl = wins + losses
            win_rate = (wins / total_with_pnl * 100) if total_with_pnl else 0
            no_diag = sum(1 for r in closed_recent if not r.get("diagnosis"))
            with st.expander(
                f"📒 已平仓 ({len(closed_recent)}) ｜ 胜率 {win_rate:.0f}% "
                f"({wins}/{total_with_pnl}) ｜ 待复盘 {no_diag}"
            ):
                rows = []
                for r in closed_recent:
                    o = r.get("open", {}) or {}
                    c = r.get("close", {}) or {}
                    pnl_pct = c.get("realized_pnl_pct")
                    rows.append({
                        "代码": r.get("ts_code", ""),
                        "名称": r.get("name", ""),
                        "策略": o.get("strategy", "—"),
                        "regime": o.get("regime_at_open", "—"),
                        "进场日": o.get("entry_date", ""),
                        "进场价": o.get("entry_price"),
                        "出场日": c.get("exit_date", ""),
                        "出场价": c.get("actual_exit_price"),
                        "盈亏%": round(pnl_pct * 100, 2) if pnl_pct is not None else None,
                        "盈亏额": c.get("realized_pnl_amount"),
                        "持有天数": c.get("days_held"),
                        "原因": c.get("exit_reason", ""),
                        "AI verdict": o.get("ai_verdict", ""),
                        "复盘": "✓" if r.get("diagnosis") else "—",
                    })
                df_closed = pd.DataFrame(rows)
                st.dataframe(
                    df_closed.style.map(
                        lambda v: "color: red" if isinstance(v, (int, float)) and v > 0
                        else ("color: green" if isinstance(v, (int, float)) and v < 0 else ""),
                        subset=["盈亏%", "盈亏额"],
                    ),
                    width="stretch",
                    hide_index=True,
                )
                st.caption("注：A股标准 红色=盈利，绿色=亏损。")

                # 单笔详情 + AI 复盘
                pick_options = {
                    f"{r.get('ts_code','')} {r.get('name','')} ｜ 出场 {(r.get('close') or {}).get('exit_date','')}"
                    f" ｜ {round((r.get('close') or {}).get('realized_pnl_pct',0) * 100, 2)}%"
                    f" {'✓' if r.get('diagnosis') else '— 未复盘'}": r
                    for r in closed_recent
                }
                pick_label = st.selectbox(
                    "查看 / 复盘", [""] + list(pick_options.keys()), key="closed_pick",
                )
                if pick_label:
                    rec = pick_options[pick_label]
                    diag = rec.get("diagnosis")
                    closed_at = (rec.get("close") or {}).get("closed_at", "")
                    if diag:
                        st.markdown(
                            f"**outcome**: {diag.get('outcome_class','?')} ｜ "
                            f"**bucket**: {diag.get('calibration_bucket','?')} ｜ "
                            f"**target_dist_max**: "
                            f"{(diag.get('target_distance_max_pct') or 0) * 100:+.2f}% ｜ "
                            f"**stop_too_tight**: {diag.get('stop_hit_too_tight')}"
                        )
                        if diag.get("ai_diagnosis_text"):
                            st.markdown("**AI 综合诊断**")
                            st.write(diag["ai_diagnosis_text"])
                        if diag.get("ai_correctly_identified"):
                            st.markdown("**当时判断对的点**")
                            for pt in diag["ai_correctly_identified"]:
                                st.markdown(f"- ✓ {pt}")
                        if diag.get("ai_missed"):
                            st.markdown("**当时漏掉 / 误判的点**")
                            for pt in diag["ai_missed"]:
                                st.markdown(f"- ✗ {pt}")
                        if diag.get("lesson"):
                            st.info(f"💡 **lesson**: {diag['lesson']}")
                        st.caption(
                            f"复盘模型 {diag.get('model','?')} ｜ 用了出场后 "
                            f"{diag.get('post_exit_kline_used','?')} 个交易日 K 线"
                        )
                        rerun_col, _ = st.columns([1, 4])
                        if rerun_col.button("重新复盘", key=f"rerun_pm_{closed_at}"):
                            with st.spinner("AI 重新复盘中…"):
                                new_diag = postmortem.run_and_patch(rec)
                            if new_diag:
                                st.success("✓ 已重新复盘")
                                st.rerun()
                            else:
                                st.error("复盘失败，请稍后重试")
                    else:
                        st.warning("此笔交易尚未复盘。")
                        if st.button("运行 AI 复盘", key=f"run_pm_{closed_at}", type="primary"):
                            with st.spinner("AI 复盘中（约 10-20 秒）…"):
                                new_diag = postmortem.run_and_patch(rec)
                            if new_diag:
                                st.success("✓ 复盘已保存")
                                st.rerun()
                            else:
                                st.error("AI 复盘失败，可稍后重试")

    with col_right:
        st.write("")
        if st.button("＋ 添加", type="primary", use_container_width=True, key="open_add_dlg"):
            _show_add_dialog()


# ── Tab 2: Analyze ────────────────────────────────────────────────────────────
with tab_analyze:
    st.subheader("个股 AI 分析")
    st.caption("调用 DeepSeek agent 自主拉取数据并给出判断")

    col_in, col_out = st.columns([1, 2])

    with col_in:
        ts_code_raw = st.text_input("股票代码", placeholder="002050.SZ", key="analyze_code")
        ts_code_analyze = data.normalize_ts_code(ts_code_raw) if ts_code_raw else ""
        if ts_code_raw and ts_code_analyze != ts_code_raw.strip().upper():
            st.caption(f"→ 自动补全: {ts_code_analyze}")
        save_journal = st.checkbox("保存到日志", value=True)
        run_btn = st.button("▶ 开始分析", type="primary", disabled=not ts_code_analyze)

        # Show last journal entry for this stock
        if ts_code_analyze:
            last = journal.load_latest(ts_code_analyze)
            if last:
                st.markdown("---")
                analyzed_at = last.get("analyzed_at", "")
                ts_label = analyzed_at.replace("T", " ")[:16] if analyzed_at else last.get("date", "?")
                st.caption(f"上次记录 {ts_label}")
                v = last.get("verdict", "")
                st.markdown(verdict_badge(v))
                pa = last.get("price_advice") or {}
                if pa.get("entry"):
                    st.caption(f"建议买入 {pa['entry']}  止损 {pa.get('stop_loss','?')}  目标 {pa.get('target','?')}")

        # Trace stream lives in the left column so the right column's final result
        # stays in view without scrolling.
        if run_btn and ts_code_analyze:
            st.markdown("---")
            st.markdown("### 🛰 分析过程")
            st.caption("每一步默认折叠，点击展开看入参 / 返回 / AI 思考")
            trace_status_container = st.container()
            trace_container = st.container()
        else:
            trace_status_container = None
            trace_container = None

    with col_out:
        if run_btn and ts_code_analyze:
            code = ts_code_analyze

            with trace_status_container:
                status = st.status(f"分析 {code} 中...", expanded=True)

            def on_progress(event):
                with trace_container:
                    render_trace_event(event)
                t = event.get("type", "")
                if t == "tool_call":
                    status.write(f"🔧 调用 {event.get('name', '?')}")
                elif t == "tool_result":
                    status.write(f"  ↳ 返回 {event.get('name', '?')}")
                elif t == "assistant_text":
                    status.write(f"💭 AI 思考 (iter {event.get('iteration', '?')})")
                elif t == "context":
                    status.write(f"🌐 注入 {event.get('name', '?')} context")
                elif t == "verdict_recorded":
                    status.write(f"📝 记录结论 {event.get('verdict', '?')}")
                elif t == "verdict_rejected":
                    status.write(f"⛔ 结论被拒（缺类别）")

            try:
                from apex import analyze as ana
                result = ana.run(code, save=save_journal, on_progress=on_progress)
                st.session_state["last_analysis"] = result
                status.update(label=f"✓ 分析完成", state="complete")
            except Exception as e:
                status.update(label=f"✗ 分析失败: {e}", state="error")
                st.error(str(e))
                result = None

            if result:
                verdict = result.get("verdict", "")
                conf = result.get("confidence", "?")
                cal_conf = result.get("calibrated_confidence")
                cal_why = result.get("calibration_explanation", "")
                color = VERDICT_COLOR.get(verdict, "gray")

                header = f"## {verdict_badge(verdict)}  置信度 {conf}/10"
                if cal_conf is not None and isinstance(cal_conf, (int, float)) and abs(cal_conf - (conf or 0)) >= 0.1:
                    header += f"  →  **校准 {cal_conf}/10**"
                st.markdown(header)
                if cal_why:
                    st.caption(f"📐 校准: {cal_why}")

                pa = result.get("price_advice") or {}
                if any(pa.values()):
                    m1, m2, m3 = st.columns(3)
                    m1.metric("建议买入", pa.get("entry", "-"))
                    m2.metric("止损", pa.get("stop_loss", "-"))
                    m3.metric("目标价", pa.get("target", "-"))

                feats = result.get("features") or {}
                # 蜡烛图形态可视化标签
                candle_parts = []
                if feats.get("candle_direction"):
                    dir_emoji = {"阳": "🟢", "阴": "🔴", "十字星": "⚪"}.get(feats["candle_direction"], "")
                    candle_parts.append(f"{dir_emoji} {feats['candle_direction']}线")
                if feats.get("candle_pattern") and feats["candle_pattern"] != "无":
                    pattern_emoji = {
                        "长上影": "📌", "长下影": "📍", "锤子线": "🔨",
                        "射击之星": "⭐", "双顶雏形": "⚠️", "量价背离": "📉",
                        "连续阴": "🔻",
                    }
                    emoji = "🕯️"
                    for k, v in pattern_emoji.items():
                        if k in str(feats["candle_pattern"]):
                            emoji = v
                            break
                    candle_parts.append(f"{emoji} {feats['candle_pattern']}")
                if feats.get("candle_body_pct") is not None:
                    candle_parts.append(f"实体{feats['candle_body_pct']}%")
                if feats.get("candle_upper_shadow_pct") is not None:
                    candle_parts.append(f"上影{feats['candle_upper_shadow_pct']}%")
                if feats.get("candle_lower_shadow_pct") is not None:
                    candle_parts.append(f"下影{feats['candle_lower_shadow_pct']}%")
                if candle_parts:
                    st.caption("🕯️ K线形态：" + " ｜ ".join(candle_parts))

                _render_intraday_chart(result.get("ts_code", ts_code_analyze))

                if feats:
                    with st.expander("技术特征"):
                        feat_df = pd.DataFrame([feats]).T.rename(columns={0: "值"})
                        feat_df["值"] = feat_df["值"].astype(str)
                        st.dataframe(feat_df, width="stretch")

                if result.get("analysis_text"):
                    with st.expander("完整分析", expanded=True):
                        st.markdown(result["analysis_text"])

                analyzed_at_ev = result.get("analyzed_at") or ""
                if analyzed_at_ev:
                    if st.button(
                        "🛰 查看 AI 分析过程",
                        key=f"trace_btn_live_{result.get('ts_code', '')}_{analyzed_at_ev}",
                    ):
                        _show_trace_dialog(result.get("ts_code", ""), analyzed_at_ev)

        elif "last_analysis" in st.session_state and not run_btn:
            r = st.session_state["last_analysis"]
            if r.get("ts_code") == ts_code_analyze:
                result = r
                pa = result.get("price_advice") or {}
                conf = result.get("confidence", "?")
                cal_conf = result.get("calibrated_confidence")
                header = f"## {verdict_badge(result.get('verdict',''))}  置信度 {conf}/10"
                if cal_conf is not None and isinstance(cal_conf, (int, float)) and abs(cal_conf - (conf or 0)) >= 0.1:
                    header += f"  →  **校准 {cal_conf}/10**"
                st.markdown(header)
                if pa.get("entry"):
                    m1, m2, m3 = st.columns(3)
                    m1.metric("建议买入", pa.get("entry", "-"))
                    m2.metric("止损", pa.get("stop_loss", "-"))
                    m3.metric("目标价", pa.get("target", "-"))
                _render_intraday_chart(result.get("ts_code", ts_code_analyze))
                if result.get("analysis_text"):
                    with st.expander("分析内容", expanded=False):
                        st.markdown(result["analysis_text"])

                analyzed_at_ev = result.get("analyzed_at") or ""
                if analyzed_at_ev:
                    if st.button(
                        "🛰 查看 AI 分析过程",
                        key=f"trace_btn_rerun_{result.get('ts_code', '')}_{analyzed_at_ev}",
                    ):
                        _show_trace_dialog(result.get("ts_code", ""), analyzed_at_ev)

        # ── Add to Watchlist (shown after any analysis, persists across rerenders) ──
        r = st.session_state.get("last_analysis")
        if r and r.get("ts_code") == ts_code_analyze:
            st.markdown("---")
            pa_r = r.get("price_advice") or {}
            entry_advice = pa_r.get("entry")
            stop_advice = pa_r.get("stop_loss")
            target_advice = pa_r.get("target")

            def _resolve_name(code: str) -> str:
                try:
                    info_raw = data.get_stock_info(ts_code=code)
                    info_list = json.loads(info_raw)
                    if isinstance(info_list, list) and info_list:
                        return info_list[0].get("name", "")
                except Exception:
                    pass
                return ""

            verdict = r.get("verdict", "")
            is_bullish = verdict in ("看多", "偏多", "观望偏多")
            st.caption("加为候选 = 还没买，等触发；已成交 = 已经买入，直接记为持仓")
            cand_col, pos_col = st.columns(2)

            # ── AI 没给 entry 但偏多时，让用户手动填 ──
            if not entry_advice and is_bullish:
                manual_trigger = cand_col.number_input(
                    "触发价（AI 未给具体价位，手动输入）",
                    min_value=0.0, step=0.01, value=0.0,
                    key=f"manual_trigger_{r['ts_code']}",
                )
                can_candidate = manual_trigger > 0
                trigger_price = manual_trigger if can_candidate else None
                note_template = f"AI偏多但未给价位，手动设触发 {manual_trigger}（止损 {stop_advice or '-'}  目标 {target_advice or '-'}）"
            else:
                can_candidate = bool(entry_advice)
                trigger_price = entry_advice
                note_template = f"AI建议买入 {entry_advice}（止损 {stop_advice or '-'}  目标 {target_advice or '-'}）" if entry_advice else ""

            if cand_col.button("加为候选（等触发）", disabled=not can_candidate):
                name = _resolve_name(r["ts_code"])
                try:
                    wl_mod.add_candidate(
                        r["ts_code"], name,
                        trigger_price=trigger_price,
                        trigger_direction="below",
                        note=note_template,
                        expires_days=7,
                        stop_advice=stop_advice,
                        target_advice=target_advice,
                    )
                    st.session_state.pop("prices", None)
                    st.success(f"✓ 已加为候选 {r['ts_code']} {name}（触发价 {trigger_price}）")
                except Exception as e:
                    st.error(f"添加失败: {e}")

            with pos_col:
                # ── AI 没给 entry 时让用户手动填成交价 ──
                if not entry_advice and is_bullish:
                    manual_entry = st.number_input(
                        "成交价（AI 未给，手动输入）",
                        min_value=0.0, step=0.01, value=0.0,
                        key=f"manual_entry_{r['ts_code']}",
                    )
                    manual_stop = st.number_input(
                        "止损价",
                        min_value=0.0, step=0.01, value=0.0,
                        key=f"manual_stop_{r['ts_code']}",
                    )
                    manual_target = st.number_input(
                        "目标价",
                        min_value=0.0, step=0.01, value=0.0,
                        key=f"manual_target_{r['ts_code']}",
                    )
                    used_entry = manual_entry if manual_entry > 0 else None
                    used_stop = manual_stop if manual_stop > 0 else None
                    used_target = manual_target if manual_target > 0 else None
                else:
                    used_entry = entry_advice
                    used_stop = stop_advice
                    used_target = target_advice

                # 仓位预览（基于 AI 建议价位 或 手动输入价位）
                analyze_acc = account_mod.load()
                analyze_sizing = (
                    account_mod.compute_position_size(used_entry, used_stop, account=analyze_acc)
                    if used_entry and used_stop else
                    {"ok": False, "shares": 0, "warnings": []}
                )
                if analyze_sizing["ok"]:
                    st.caption(
                        f"建议 **{analyze_sizing['shares']}** 股 ｜ "
                        f"占资金 {analyze_sizing['capital_required']:,.0f} ｜ "
                        f"风险 {analyze_sizing['risk_amount']:,.0f}"
                        f"（{analyze_sizing['risk_pct_actual']}%）"
                    )

                analyze_shares = st.number_input(
                    "成交手数",
                    min_value=0, step=100,
                    value=analyze_sizing.get("shares", 0) or 0,
                    key=f"analyze_shares_{r['ts_code']}",
                )

                if st.button("已成交，记为持仓"):
                    name = _resolve_name(r["ts_code"])
                    actual_risk = (
                        analyze_shares * abs(used_entry - used_stop)
                        if analyze_shares and used_entry and used_stop else None
                    )
                    pos_args = dict(
                        ts_code=r["ts_code"], name=name,
                        entry_price=used_entry,
                        stop_loss=used_stop,
                        target=used_target,
                        expires_days=10,
                        position_size_shares=analyze_shares or None,
                        risk_amount=actual_risk,
                        calibrated_confidence=r.get("calibrated_confidence"),
                        strategy="analyze",
                    )
                    try:
                        wl_mod.add_position(**pos_args)
                        st.session_state.pop("prices", None)
                        st.success(f"✓ 已记为持仓 {r['ts_code']} {name}，切换到 Watchlist 标签查看")
                    except wl_mod.DuplicatePositionError as e:
                        st.session_state["_dup_pos"] = {
                            "ts_code": r["ts_code"],
                            "existing": e.existing,
                            "args": pos_args,
                        }
                        st.warning(f"⚠ 持仓 {r['ts_code']} 已存在，请到 Watchlist 标签确认替换或取消")
                    except Exception as e:
                        st.error(f"添加失败: {e}")

    # ── History (full-width, below columns) ─────────────────────────────────
    if ts_code_analyze:
        history = sorted(
            journal.load_entries(ts_code_analyze),
            key=lambda e: e.get("analyzed_at") or e.get("date", ""),
            reverse=True,
        )
        if history:
            st.markdown("---")
            with st.expander(
                f"📜 {ts_code_analyze} 历史分析（{len(history)} 条）",
                expanded=False,
            ):
                labels = []
                for ent in history:
                    ts = ent.get("analyzed_at") or ent.get("date") or "?"
                    ts_disp = str(ts).replace("T", " ")[:16] if "T" in str(ts) else str(ts)
                    v_h = ent.get("verdict", "?")
                    conf = ent.get("confidence", "?")
                    labels.append(f"{ts_disp}  ·  {v_h}  ·  置信 {conf}/10")

                pick = st.radio(
                    "选择记录",
                    list(range(len(history))),
                    format_func=lambda i: labels[i],
                    key=f"hist_pick_{ts_code_analyze}",
                    label_visibility="collapsed",
                )

                ent = history[pick]
                ts = ent.get("analyzed_at") or ent.get("date") or "?"
                ts_disp = str(ts).replace("T", " ")[:16] if "T" in str(ts) else str(ts)
                v_h = ent.get("verdict", "")
                conf = ent.get("confidence", "?")
                pa_h = ent.get("price_advice") or {}

                st.markdown(
                    f"#### {ts_disp} — {verdict_badge(v_h)} 置信度 {conf}/10"
                )
                if isinstance(pa_h, dict) and any(pa_h.values()):
                    m1, m2, m3 = st.columns(3)
                    m1.metric("建议买入", pa_h.get("entry") or "-")
                    m2.metric("止损", pa_h.get("stop_loss") or "-")
                    m3.metric("目标", pa_h.get("target") or "-")

                feats = ent.get("features") or {}
                # 蜡烛图形态标签（历史记录）
                candle_parts_h = []
                if feats.get("candle_direction"):
                    dir_emoji = {"阳": "🟢", "阴": "🔴", "十字星": "⚪"}.get(feats["candle_direction"], "")
                    candle_parts_h.append(f"{dir_emoji} {feats['candle_direction']}线")
                if feats.get("candle_pattern") and feats["candle_pattern"] != "无":
                    candle_parts_h.append(f"🕯️ {feats['candle_pattern']}")
                if feats.get("candle_body_pct") is not None:
                    candle_parts_h.append(f"实体{feats['candle_body_pct']}%")
                if feats.get("candle_upper_shadow_pct") is not None:
                    candle_parts_h.append(f"上影{feats['candle_upper_shadow_pct']}%")
                if feats.get("candle_lower_shadow_pct") is not None:
                    candle_parts_h.append(f"下影{feats['candle_lower_shadow_pct']}%")
                if candle_parts_h:
                    st.caption("🕯️ K线形态：" + " ｜ ".join(candle_parts_h))

                if feats:
                    st.markdown("**技术特征**")
                    feat_df = pd.DataFrame([feats]).T.rename(columns={0: "值"})
                    feat_df["值"] = feat_df["值"].astype(str)
                    st.dataframe(feat_df, width="stretch")

                text = ent.get("analysis_text") or ""
                if text:
                    st.markdown("**完整分析**")
                    st.markdown(text)

                # 分析过程（trace）按钮 —— dialog 弹出，避免嵌套 expander
                analyzed_at_full = ent.get("analyzed_at") or ""
                if analyzed_at_full:
                    if st.button(
                        "🛰 查看 AI 分析过程",
                        key=f"hist_trace_btn_{ts_code_analyze}_{pick}",
                    ):
                        _show_trace_dialog(ts_code_analyze, analyzed_at_full)


# ── Tab 3: Backtest ──────────────────────────────────────────────────────────
with tab_bt:
    from apex import backtest as bt_mod

    # ── Section A: 实际平仓分析 ──────────────────────────────────────────────
    st.subheader("实际平仓分析")
    st.caption("读取 closed_positions.jsonl — 真实成交，无模拟。净收益 = 毛利 − 佣金(万2.5×2) − 印花税(千1)")

    closed_recs = wl_mod.load_closed_positions()
    if closed_recs:
        rz_df = bt_mod.run_realized(closed_recs)

        # Top-level metrics
        valid = rz_df[rz_df["net_pnl_pct"].notna()]
        if not valid.empty:
            rc1, rc2, rc3, rc4 = st.columns(4)
            rc1.metric("已平仓笔数", len(valid))
            rc2.metric("胜率（净）", f"{valid['hit'].mean():.1%}")
            rc3.metric("平均净收益", f"{valid['net_pnl_pct'].mean():.2%}")
            rc4.metric("平均持仓天数", f"{valid['days_held'].mean():.1f} 天")

        # Attribution tables: strategy + regime side by side
        col_s, col_r = st.columns(2)
        with col_s:
            st.markdown("**策略归因**")
            grp_s = (
                valid.groupby("strategy")
                .agg(笔数=("hit", "count"), 胜率=("hit", "mean"), 均净收益=("net_pnl_pct", "mean"))
                .reset_index()
                .sort_values("均净收益", ascending=False)
            )
            grp_s["胜率"] = grp_s["胜率"].map("{:.0%}".format)
            grp_s["均净收益"] = grp_s["均净收益"].map("{:+.2%}".format)
            st.dataframe(grp_s.rename(columns={"strategy": "策略"}), hide_index=True)

        with col_r:
            st.markdown("**Regime 归因**")
            grp_r = (
                valid.groupby("regime")
                .agg(笔数=("hit", "count"), 胜率=("hit", "mean"), 均净收益=("net_pnl_pct", "mean"))
                .reset_index()
                .sort_values("均净收益", ascending=False)
            )
            grp_r["胜率"] = grp_r["胜率"].map("{:.0%}".format)
            grp_r["均净收益"] = grp_r["均净收益"].map("{:+.2%}".format)
            st.dataframe(grp_r.rename(columns={"regime": "市场状态"}), hide_index=True)

        # Exit reason breakdown
        st.markdown("**出场原因分布**")
        grp_e = (
            valid.groupby("exit_reason")
            .agg(笔数=("hit", "count"), 胜率=("hit", "mean"), 均净收益=("net_pnl_pct", "mean"))
            .reset_index()
            .sort_values("笔数", ascending=False)
        )
        grp_e["胜率"] = grp_e["胜率"].map("{:.0%}".format)
        grp_e["均净收益"] = grp_e["均净收益"].map("{:+.2%}".format)
        st.dataframe(grp_e.rename(columns={"exit_reason": "出场原因"}), hide_index=True)

        # Equity curve (cumulative realized P&L across time)
        if len(valid) >= 2:
            eq_df = valid.sort_values("exit_date").copy()
            eq_df["累计净收益"] = (1 + eq_df["net_pnl_pct"]).cumprod() - 1
            fig_eq = px.line(
                eq_df, x="exit_date", y="累计净收益",
                labels={"exit_date": "平仓日期", "累计净收益": "累计净收益"},
                title="实际平仓累计净收益曲线（非等权组合，仅供参考）",
            )
            fig_eq.update_traces(line_color="#e84040")
            fig_eq.update_yaxes(tickformat=".1%")
            st.plotly_chart(fig_eq, use_container_width=True)

        # Detail table
        with st.expander("明细数据"):
            disp = rz_df[["ts_code", "name", "strategy", "regime", "entry_date",
                           "exit_date", "exit_reason", "days_held",
                           "gross_pnl_pct", "net_pnl_pct", "hit"]].copy()
            disp["gross_pnl_pct"] = disp["gross_pnl_pct"].apply(
                lambda v: f"{v:+.2%}" if v is not None and not (isinstance(v, float) and pd.isna(v)) else "—"
            )
            disp["net_pnl_pct"] = disp["net_pnl_pct"].apply(
                lambda v: f"{v:+.2%}" if v is not None and not (isinstance(v, float) and pd.isna(v)) else "—"
            )
            disp["hit"] = disp["hit"].map({True: "盈", False: "亏", None: "—"})
            st.dataframe(
                disp.rename(columns={
                    "ts_code": "代码", "name": "名称", "strategy": "策略", "regime": "市场状态",
                    "entry_date": "开仓日", "exit_date": "平仓日", "exit_reason": "原因",
                    "days_held": "持仓天", "gross_pnl_pct": "毛收益", "net_pnl_pct": "净收益",
                    "hit": "盈亏",
                }),
                hide_index=True,
            )
    else:
        st.info("暂无已平仓记录。完成第一笔平仓后，实际分析将显示在此处。")

    st.markdown("---")

    # ── Section B: 信号模拟回测 ──────────────────────────────────────────────
    st.subheader("信号模拟回测")
    st.caption("以 AI 看多 verdict 为信号，T+1 收盘价入场 · 含佣金印花税 · 对比沪深300")

    col_ctrl, _ = st.columns([1, 3])
    with col_ctrl:
        bt_code = st.text_input("只看某只股票（留空=全部）", key="bt_code")
        bt_days = st.slider("持仓天数", 3, 30, 10)
        run_bt = st.button("▶ 运行回测", type="primary")

    if run_bt:
        with st.spinner("回测中（含基准拉取，稍慢）..."):
            df = bt_mod.run(ts_code=data.normalize_ts_code(bt_code) or None, lookforward_days=bt_days)
            st.session_state["bt_df"] = df

    df = st.session_state.get("bt_df")
    if df is not None and not df.empty:
        st.markdown("---")
        has_bench = "benchmark_return" in df.columns and df["benchmark_return"].notna().any()
        avg_ret = df["net_return"].mean()
        avg_bench = df["benchmark_return"].mean() if has_bench else None

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("多头信号数", len(df))
        m2.metric("胜率（净）", f"{df['hit'].mean():.1%}")
        m3.metric("平均净收益", f"{avg_ret:.2%}",
                  delta=f"超额 {avg_ret - avg_bench:.2%}" if avg_bench is not None else None)
        if has_bench:
            beat_pct = df["beat_benchmark"].mean() if "beat_benchmark" in df.columns else None
            m4.metric("跑赢基准率", f"{beat_pct:.1%}" if beat_pct is not None else "—",
                      delta=f"基准均值 {avg_bench:.2%}")
        elif df["max_drawdown"].notna().any():
            m4.metric("平均最大回撤", f"{df['max_drawdown'].mean():.2%}")

        # Return distribution chart with benchmark scatter
        df_plot = df.copy()
        df_plot["label"] = df_plot["ts_code"] + " " + df_plot["date"].str[5:]
        df_plot["color"] = df_plot["hit"].map({True: "盈利", False: "亏损"})
        df_plot["return_pct"] = (df_plot["net_return"] * 100).round(2)
        df_plot = df_plot.sort_values("net_return")

        fig = px.bar(
            df_plot,
            x="return_pct", y="label",
            color="color",
            color_discrete_map={"盈利": "#e84040", "亏损": "#1da462"},
            orientation="h",
            labels={"return_pct": "净收益率 (%)", "label": ""},
            title=f"各信号净收益（T+1 入场 · {bt_days} 天持仓 · 含成本）",
            height=max(300, len(df) * 28),
        )
        if has_bench:
            # Overlay benchmark dots
            df_plot["bench_pct"] = (df_plot["benchmark_return"] * 100).round(2)
            fig.add_scatter(
                x=df_plot["bench_pct"], y=df_plot["label"],
                mode="markers", marker=dict(color="#636efa", size=6, symbol="diamond"),
                name="同期沪深300",
            )
        fig.update_layout(showlegend=True, margin=dict(l=120))
        st.plotly_chart(fig, use_container_width=True)

        # Strategy attribution (if strategy column has data)
        if "strategy" in df.columns and df["strategy"].str.strip().any():
            st.markdown("**策略归因（模拟）**")
            grp_bs = (
                df.groupby("strategy")
                .agg(信号数=("hit", "count"), 胜率=("hit", "mean"), 均净收益=("net_return", "mean"))
                .reset_index()
                .sort_values("均净收益", ascending=False)
            )
            grp_bs["胜率"] = grp_bs["胜率"].map("{:.0%}".format)
            grp_bs["均净收益"] = grp_bs["均净收益"].map("{:+.2%}".format)
            st.dataframe(grp_bs.rename(columns={"strategy": "策略"}), hide_index=True)

        # Detail table
        with st.expander("详细数据"):
            detail = df.copy()
            for col_name in ["net_return", "benchmark_return", "excess_return"]:
                if col_name in detail.columns:
                    detail[col_name] = detail[col_name].apply(
                        lambda v: f"{v:+.2%}" if v is not None and not (isinstance(v, float) and pd.isna(v)) else "—"
                    )
            if "max_drawdown" in detail.columns and detail["max_drawdown"].notna().any():
                detail["max_drawdown"] = detail["max_drawdown"].apply(
                    lambda v: f"{v:.2%}" if v is not None and not (isinstance(v, float) and pd.isna(v)) else "—"
                )
            if "hit" in detail.columns:
                detail["hit"] = detail["hit"].map({True: "盈", False: "亏"})
            if "beat_benchmark" in detail.columns:
                detail["beat_benchmark"] = detail["beat_benchmark"].map(
                    {True: "跑赢", False: "跑输", None: "—"}
                )
            detail = detail.rename(columns={
                "ts_code": "代码", "date": "信号日", "analyzed_at": "分析时间",
                "verdict": "判断", "confidence": "置信度", "strategy": "策略",
                "fill_price": "入场价(T+1)", "net_return": "净收益",
                "benchmark_return": "沪深300", "excess_return": "超额",
                "max_drawdown": "最大回撤", "sharpe": "夏普",
                "hit": "盈亏", "beat_benchmark": "vs基准", "has_features": "有特征",
            })
            st.dataframe(detail, hide_index=True)

    elif df is not None and df.empty:
        msg = "没有找到可回测的多头信号记录。"
        if bt_code.strip():
            entries = journal.load_entries(ts_code=bt_code.strip())
            if entries:
                verdicts = "、".join(set(e.get("verdict", "?") for e in entries))
                msg += f"\n\n该股票有 {len(entries)} 条记录，但判断均为非多头（{verdicts}），回测仅处理看多信号。"
            else:
                msg += f"\n\n未找到 {bt_code.strip()} 的日志文件。"
        st.warning(msg)


# ── Tab 4: Screener (今日粗筛) ────────────────────────────────────────────────

_SIGNAL_ICONS = {
    "dragon_tiger": "🐉",
    "limit_up": "🚀",
    "industry": "🔥",
    "northbound": "🌊",
    "concept": "💎",
    "limit_up_history": "📜",
}
_SIGNAL_LABELS = {
    "dragon_tiger": "龙虎榜",
    "limit_up": "涨停",
    "industry": "强势行业",
    "northbound": "北向加仓",
    "concept": "概念龙头",
    "limit_up_history": "近期涨停",
}


def _signal_icons(types: list[str]) -> str:
    return " ".join(_SIGNAL_ICONS.get(t, "•") for t in types)


with tab_screen:
    from apex import screener as screener_mod

    st.subheader("今日粗筛 — 多策略 → AI 复核（含 actionable）")
    st.caption(
        "6 信号源（含近20日涨停历史）→ 7 个策略 select+score（首板/机构/轮动/龙头放量/"
        "**回踩均线/多头排列/放量突破**）→ 按权重融合 Top N → AI 60s 打分。"
        "下面可手动选择策略组合和权重。"
    )

    # ── 策略组合配置 ──────────────────────────────────────────
    from apex.strategies import STRATEGIES as _STRAT_MODULES

    _STRAT_CN = {
        "first_board_leader": "首板龙头",
        "institutional_flow": "机构资金",
        "industry_rotation": "行业轮动",
        "leader_with_volume": "龙头放量",
        "pullback_to_ma": "回踩均线",
        "bullish_alignment": "多头排列",
        "volume_breakout": "放量突破",
    }
    _STRAT_ICONS = {
        "first_board_leader": "🚀",
        "institutional_flow": "🏦",
        "industry_rotation": "🔄",
        "leader_with_volume": "🐉",
        "pullback_to_ma": "📉",
        "bullish_alignment": "📈",
        "volume_breakout": "💥",
    }

    _PRESET_AUTO = "均衡（AI 自动选权重）"
    _PRESETS = [
        _PRESET_AUTO,
        "稳健（技术面+机构）",
        "激进（打板+放量）",
        "纯技术面（均线+量能）",
        "自定义",
    ]
    _PRESET_WEIGHTS = {
        "稳健（技术面+机构）": {
            "pullback_to_ma": 0.28, "bullish_alignment": 0.22,
            "institutional_flow": 0.22, "industry_rotation": 0.16,
            "volume_breakout": 0.12, "first_board_leader": 0.0,
            "leader_with_volume": 0.0,
        },
        "激进（打板+放量）": {
            "first_board_leader": 0.25, "leader_with_volume": 0.25,
            "volume_breakout": 0.20, "pullback_to_ma": 0.12,
            "bullish_alignment": 0.10, "institutional_flow": 0.05,
            "industry_rotation": 0.03,
        },
        "纯技术面（均线+量能）": {
            "pullback_to_ma": 0.30, "bullish_alignment": 0.28,
            "volume_breakout": 0.22, "industry_rotation": 0.20,
            "first_board_leader": 0.0, "leader_with_volume": 0.0,
            "institutional_flow": 0.0,
        },
    }

    # Default: AI auto
    strategy_weights: dict | None = None

    with st.expander("📐 策略组合配置", expanded=True):
        preset = st.selectbox("预设组合方案", _PRESETS, key="_screen_preset")

        if preset == _PRESET_AUTO:
            st.caption(
                "全部 7 个策略启用，AI 根据当前市场 regime + 历史胜率自动分配权重。"
                "勾选「仅规则层（跳过 AI）」时仍使用 AI 选权（未被跳过）。"
            )
        else:
            if preset == "自定义":
                weights = st.session_state.get("_screen_custom_weights", {})
                if not weights:
                    eq = round(1.0 / len(_STRAT_MODULES), 4)
                    weights = {n: eq for n in _STRAT_MODULES}
            else:
                weights = dict(_PRESET_WEIGHTS.get(preset, {}))
                for name in _STRAT_MODULES:
                    weights.setdefault(name, 0.0)

            st.caption("勾选 = 启用，拖拽滑块调权重。按任意值调整，提交时自动归一化。")

            for name in _STRAT_MODULES:
                label = _STRAT_CN.get(name, name)
                icon = _STRAT_ICONS.get(name, "")
                cur_w = weights.get(name, 0.0)
                enabled = cur_w > 0

                c1, c2, c3, c4 = st.columns([1, 3, 2, 4])
                c1.write(icon)
                c2.write(f"**{label}**")

                if preset == "自定义":
                    en = c3.checkbox(
                        "", value=enabled,
                        label_visibility="collapsed",
                        key=f"_sc_en_{name}",
                    )
                    if en:
                        w = c4.slider(
                            "", 0.0, 1.0, value=max(cur_w, 0.05), step=0.05,
                            label_visibility="collapsed",
                            key=f"_sc_w_{name}",
                        )
                    else:
                        w = 0.0
                        c4.caption("已关闭")
                else:
                    c3.write("✅" if enabled else "—")
                    c4.write(f"{cur_w:.0%}")

                weights[name] = w

            total_w = sum(v for v in weights.values() if v > 0)
            if total_w <= 0:
                st.warning("至少启用一个策略！")
            else:
                norm = {n: round(v / total_w, 4) for n, v in weights.items()}
                n_enabled = sum(1 for v in weights.values() if v > 0)
                if preset == "自定义":
                    st.session_state["_screen_custom_weights"] = weights
                weight_desc = ", ".join(
                    f"{_STRAT_CN.get(n, n)}={w:.0%}"
                    for n, w in norm.items() if w > 0
                )
                st.caption(f"启用 {n_enabled}/{len(_STRAT_MODULES)} 个 ｜ {weight_desc}")

                if preset != _PRESET_AUTO:
                    strategy_weights = norm

    available_dates = screener_mod.list_available_dates()

    ctrl_col, run_col = st.columns([3, 1])
    with ctrl_col:
        if available_dates:
            picked_date = st.selectbox(
                "查看日期",
                ["（最新）"] + available_dates,
                key="screen_date_pick",
            )
        else:
            picked_date = "（最新）"
            st.info("尚无粗筛记录，点右侧「立即重跑」生成今日粗筛。")
    with run_col:
        st.write("")
        st.write("")
        run_now = st.button("▶ 立即重跑", type="primary", key="screen_run")
        skip_ai = st.checkbox("仅规则层（跳过 AI）", key="screen_skip_ai")

    if run_now:
        progress_box = st.empty()
        msgs: list[str] = []

        def _on_progress(m: str):
            msgs.append(m)
            progress_box.code("\n".join(msgs[-12:]))

        try:
            with st.spinner("正在拉取信号 + 评分..."):
                result = screener_mod.run(
                    on_progress=_on_progress,
                    skip_ai=skip_ai,
                    strategy_weights=strategy_weights,
                )
            st.success(
                f"✓ 完成：{result['total_candidates']} 只候选 → "
                f"Top {len(result['top_scored'])}"
            )
            st.session_state["_screen_just_ran"] = True
            st.rerun()
        except Exception as e:
            st.error(f"运行失败：{type(e).__name__}: {e}")

    # Load report
    if picked_date == "（最新）":
        report = screener_mod.load_latest()
    else:
        report = screener_mod.load_by_date(picked_date)

    if not report:
        st.info("暂无数据。")
    else:
        # Header metrics
        st.markdown("---")
        st.caption(
            f"交易日：**{report.get('trade_date', '?')}** ｜ "
            f"生成时间：{report.get('generated_at', '?')[:16].replace('T', ' ')}"
        )

        # NEW: Regime banner
        regime_info = report.get("regime") or {}
        if regime_info:
            label = regime_info.get("label", "?")
            label_color = {
                "risk_on": "🟢", "risk_off": "🔴", "neutral": "🟡",
            }.get(label, "⚪")
            metrics_obj = regime_info.get("metrics") or {}
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("市场 regime", f"{label_color} {label}")
            hs = metrics_obj.get("hs300_pct")
            cs = metrics_obj.get("csi1000_pct")
            if hs is not None:
                r2.metric("沪深300", f"{hs:+.2f}%")
            if cs is not None:
                r3.metric("中证1000", f"{cs:+.2f}%")
            style = metrics_obj.get("style_small_minus_large")
            if style is not None:
                r4.metric("风格 (小-大)", f"{style:+.2f}",
                          "小盘强" if style > 0.3 else ("大盘强" if style < -0.3 else "均衡"))
            st.caption(f"💡 {regime_info.get('summary', '')}")

        # NEW: Selector reasoning
        weights_source = report.get("weights_source", "")
        selector_reasoning = report.get("selector_reasoning", "")
        if selector_reasoning:
            badge = {"ai": "🤖 AI", "manual": "✋ 手动", "equal": "⚖️ 等权",
                     "equal_fallback": "⚖️ 等权(回落)"}.get(weights_source, weights_source)
            st.info(f"**策略权重决策（{badge}）**：{selector_reasoning}")

        top_scored = report.get("top_scored", []) or []
        all_cands = report.get("all_candidates_summary") or report.get("all_candidates", []) or []
        red_flag_count = sum(1 for r in top_scored if r.get("red_flag"))
        actionable_high = sum(1 for r in top_scored if r.get("actionable") == "high")
        actionable_none = sum(1 for r in top_scored if r.get("actionable") == "none")
        avg_ai = (
            round(sum((r.get("ai_score") or 0) for r in top_scored) / len(top_scored), 1)
            if top_scored else 0
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("候选总数", len(all_cands))
        m2.metric("Top N（AI 复核）", len(top_scored))
        m3.metric("可买入 high", actionable_high)
        m4.metric("买不到 / 红旗", f"{actionable_none} / {red_flag_count}")

        # 策略权重 + 各策略候选数
        sw = report.get("strategy_weights") or {}
        bs = report.get("by_strategy") or {}
        if sw or bs:
            with st.expander("📐 策略权重 & 候选分布", expanded=False):
                if sw:
                    cols = st.columns(len(sw))
                    for (name, w), col in zip(sw.items(), cols):
                        n = (bs.get(name) or {}).get("n_candidates", 0)
                        col.metric(name, f"权重 {w*100:.0f}%", f"{n} 候选")
                if bs:
                    for sname, sinfo in bs.items():
                        with st.expander(f"{sname}: {sinfo.get('n_candidates', 0)} 候选 ｜ Top 5"):
                            top5 = sinfo.get("top_5", []) or []
                            if top5:
                                st.dataframe(
                                    pd.DataFrame([{
                                        "代码": t["ts_code"],
                                        "名称": t.get("name", ""),
                                        "规则分": t.get("strategy_score", 0),
                                        "归因": t.get("reasoning", ""),
                                    } for t in top5]),
                                    hide_index=True, width="stretch",
                                )
                            else:
                                st.caption("无候选")

        # Main table
        st.markdown("### Top 候选（按 AI 分数排序）")
        if not top_scored:
            st.info("Top 候选为空。")
        else:
            _ACTIONABLE_BADGE = {
                "high": ":green[🟢 可买]",
                "medium": ":orange[🟡 谨慎]",
                "low": ":red[🔴 难成交]",
                "none": ":gray[⛔ 买不到]",
            }
            for idx, r in enumerate(top_scored):
                ts_code = r.get("ts_code", "")
                name = r.get("name", "")
                ai_score = r.get("ai_score")
                strat = r.get("strategy", "?")
                strat_score = r.get("strategy_score", 0)
                verdict = r.get("verdict") or "-"
                one_liner = r.get("one_liner") or ""
                actionable = r.get("actionable")
                entry_strat = r.get("entry_strategy", "")
                strategy_reasoning = r.get("strategy_reasoning", "")
                red_flag = r.get("red_flag")

                badge_color = "red" if red_flag else (
                    "green" if (ai_score or 0) >= 7 else (
                        "orange" if (ai_score or 0) >= 5 else "gray"
                    )
                )
                ai_score_disp = f":{badge_color}[**{ai_score}**]" if ai_score is not None else "-"
                actionable_disp = _ACTIONABLE_BADGE.get(actionable, "")

                with st.container(border=True):
                    head_col, btn_col = st.columns([4, 2])
                    with head_col:
                        flag = " 🚩" if red_flag else ""
                        st.markdown(
                            f"**{ts_code}** {name}{flag}  "
                            f"｜ AI {ai_score_disp} ｜ 策略分 {strat_score:.2f}  "
                            f"｜ {verdict_badge(verdict)} {actionable_disp}"
                        )
                        st.caption(f"📊 策略：**{strat}** ｜ {strategy_reasoning}")
                        if one_liner:
                            st.write(one_liner)
                        if entry_strat:
                            st.info(f"🎯 入场建议：{entry_strat}")

                        with st.expander("原始信号证据"):
                            for s in r.get("signals", []):
                                stype = s.get("signal_type", "")
                                raw = s.get("raw", {})
                                st.caption(
                                    f"{_SIGNAL_ICONS.get(stype, '•')} "
                                    f"{_SIGNAL_LABELS.get(stype, stype)} "
                                    f"(强度 {s.get('signal_strength', 0)}) — "
                                    f"{json.dumps(raw, ensure_ascii=False)}"
                                )

                    with btn_col:
                        # 不可买入的不显示加候选按钮，避免误操作
                        if actionable != "none":
                            cand_btn = st.button(
                                "加为候选",
                                key=f"screen_cand_{ts_code}_{idx}",
                            )
                        else:
                            cand_btn = False
                            st.caption("⛔ 一字板/高度龙头，不建议加候选")
                        deep_btn = st.button(
                            "🔍 跳深分",
                            key=f"screen_deep_{ts_code}_{idx}",
                        )

                        if cand_btn:
                            try:
                                note = f"粗筛 AI {ai_score} ｜ 策略 {strat} ｜ {entry_strat or one_liner}"
                                wl_mod.add_candidate(
                                    ts_code, name,
                                    trigger_price=0.0,
                                    trigger_direction="above",
                                    note=note,
                                    expires_days=7,
                                    strategy=strat,
                                )
                                st.success(f"✓ 已加为候选 {ts_code}（策略 {strat}）")
                                st.session_state.pop("prices", None)
                            except Exception as e:
                                st.error(f"添加失败: {e}")

                        if deep_btn:
                            st.session_state["analyze_jump_to"] = ts_code
                            st.toast(f"已切换到「个股分析」标签，准备分析 {ts_code}")

        # Full pool (collapsed)
        with st.expander(f"全部 {len(all_cands)} 只候选（按融合权重分排序）"):
            if all_cands:
                # 兼容老 schema (signal_types/rule_score) 和新 schema (strategy/weighted_score)
                rows = []
                for c in all_cands:
                    if "strategy" in c:
                        rows.append({
                            "代码": c.get("ts_code", ""),
                            "名称": c.get("name", ""),
                            "策略": c.get("strategy", ""),
                            "策略分": c.get("strategy_score", 0),
                            "加权分": c.get("weighted_score", 0),
                        })
                    else:
                        rows.append({
                            "代码": c.get("ts_code", ""),
                            "名称": c.get("name", ""),
                            "rule_score": c.get("rule_score", 0),
                            "signals": _signal_icons(c.get("signal_types", [])),
                        })
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            else:
                st.caption("空")

        # Red flags
        red_flags = [r for r in top_scored if r.get("red_flag")]
        if red_flags:
            with st.expander(f"🚩 红旗票 ({len(red_flags)} 只)"):
                for r in red_flags:
                    st.markdown(
                        f"- **{r['ts_code']}** {r.get('name', '')} "
                        f"— {r.get('one_liner', '')}"
                    )


# ── Chat sidebar ──────────────────────────────────────────────────────────────

_CHAT_SYSTEM_PROMPT = """\
你是 Apex AI 股票分析助手，一个 A 股投资对话机器人。

## 核心能力
- 分析 A 股市场、板块、个股
- 解读行情数据、资金流向、技术指标
- 回答投资方法论问题（PE/PB/仓位管理等）

## 意图分析（重要）
用户表达往往不完整。收到问题后先判断清晰度：

**模糊问题**（如"看看今天盘面""有色板块怎么样""最近有什么机会"）：
→ 先做 1 轮澄清，追问 1-2 个关键方向。模板：
  "你想重点看哪个维度？我帮你梳理几个方向：
  • [方向1] — [一句话说明]
  • [方向2] — [一句话说明]
  • [方向3] — [一句话说明]
  或者直接说'全都要'，我一次性展开。"

**明确问题**（含股票代码/具体指标/明确指令）：
→ 直接回答。末尾可追加："还需要我分析 XX 方面吗？"（1 个扩展方向即可，不要列太多）

## 对话规则
- 回答简洁、要点式。每条结论有具体数据或逻辑支撑
- 涉及买卖建议时声明「⚠️ 仅供参考，不构成投资建议」
- 用中文回答
- 当前时间：{current_time}
"""

# Context budget: reserve ~24K tokens for conversation history (DeepSeek v4-pro = 1M window)
_CHAT_HISTORY_TOKEN_BUDGET = 24000


def _estimate_tokens(text: str) -> int:
    """Rough BPE token estimate for Chinese + English mixed text.
    Chinese chars ~1.5 tokens, ASCII ~0.3 tokens. Returns int."""
    cn = sum(1 for c in text if '一' <= c <= '鿿')
    en = len(text) - cn
    return int(cn * 1.5 + en * 0.3)


def _trim_history(history: list[dict], budget: int) -> list[dict]:
    """Keep most recent messages that fit within token budget."""
    kept = []
    total = 0
    for msg in reversed(history):
        t = _estimate_tokens(msg.get("content", ""))
        if total + t > budget and kept:
            break
        kept.insert(0, msg)
        total += t
    return kept


def _chat_reply(user_message: str, chat_history: list[dict]):
    """Stream a reply from the LLM. Yields content chunks."""
    from apex import llm

    now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S 北京时间")
    system = _CHAT_SYSTEM_PROMPT.format(current_time=now_str)

    messages = [{"role": "system", "content": system}]
    for m in _trim_history(chat_history, _CHAT_HISTORY_TOKEN_BUDGET):
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": user_message})

    try:
        yield from llm.chat_stream(messages, temperature=0.3, max_tokens=8192)
    except Exception as e:
        yield f"\n\n❌ 调用大模型失败：{e}"


def _render_chat_sidebar():
    """Render AI chat panel in the sidebar with streaming replies."""
    with st.sidebar:
        st.subheader("💬 AI 对话")

        if "_chat_messages" not in st.session_state:
            st.session_state["_chat_messages"] = []
        if "_chat_pending" not in st.session_state:
            st.session_state["_chat_pending"] = None

        # ── Toolbar ──
        has_messages = len(st.session_state["_chat_messages"]) > 0
        if has_messages:
            col1, col2 = st.columns([1, 1])
            with col1:
                if st.button("🗑️ 清空", use_container_width=True, key="clear_chat_btn"):
                    st.session_state["_chat_messages"] = []
                    st.session_state["_chat_pending"] = None
                    st.rerun()
            with col2:
                st.caption(f"{len(st.session_state['_chat_messages'])} 条消息")

        # ── Content ──
        if not has_messages and st.session_state["_chat_pending"] is None:
            st.info(
                "👋 我是 Apex AI 股票分析助手。\n\n"
                "可以问我任何 A 股相关的问题，比如：\n"
                "• 最近市场热点是什么？\n"
                "• 如何理解 PE 和 PB？\n"
                "• 当前仓位管理有什么建议？\n\n"
                "⚠️ 分析仅供参考，不构成投资建议"
            )
        else:
            pending = st.session_state["_chat_pending"]
            chat_container = st.container(height=380)
            with chat_container:
                for msg in st.session_state["_chat_messages"]:
                    with st.chat_message(msg["role"]):
                        st.markdown(msg["content"])

                # Stream assistant reply if there's a pending message
                if pending is not None:
                    st.session_state["_chat_pending"] = None
                    with st.chat_message("assistant"):
                        chunks = []
                        try:
                            for chunk in _chat_reply(
                                user_message=pending,
                                chat_history=[
                                    {"role": m["role"], "content": m["content"]}
                                    for m in st.session_state["_chat_messages"]
                                ],
                            ):
                                chunks.append(chunk)
                            reply = "".join(chunks)
                            st.markdown(reply)
                        except Exception as e:
                            reply = f"❌ 出错了：{e}"
                            st.error(reply)
                    st.session_state["_chat_messages"].append({"role": "assistant", "content": reply})
                    st.rerun()

        # ── Chat input (always last) ──
        if prompt := st.chat_input("输入问题...", key="sidebar_chat"):
            st.session_state["_chat_messages"].append({"role": "user", "content": prompt})
            st.session_state["_chat_pending"] = prompt
            st.rerun()


_render_chat_sidebar()
