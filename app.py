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

from apex import watchlist as wl_mod, journal, data

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

tab_wl, tab_analyze, tab_bt = st.tabs(["📋 Watchlist", "🔍 个股分析", "📊 回测"])


# ── Helpers ───────────────────────────────────────────────────────────────────
VERDICT_COLOR = {
    "看多": "green", "偏多": "green", "观望偏多": "green",
    "中性": "gray", "观望": "gray",
    "观望偏空": "red", "偏空": "red", "看空": "red",
}

def verdict_badge(verdict: str) -> str:
    color = VERDICT_COLOR.get(verdict, "gray")
    return f":{color}[**{verdict}**]"


# ── Tab 1: Watchlist ──────────────────────────────────────────────────────────
with tab_wl:
    col_left, col_right = st.columns([3, 2])

    with col_left:
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

        # Active positions table
        if wl_data["active_positions"]:
            st.markdown("**📊 持仓**")
            rows = []
            for p in wl_data["active_positions"]:
                cur = prices.get(p["ts_code"])
                rt = realtime_prices.get(p["ts_code"])
                ref = rt if rt is not None else cur  # 实时优先，回退昨收
                entry_p = p.get("entry_price")
                pnl = (round((ref - entry_p) / entry_p * 100, 1)
                       if ref is not None and entry_p else None)
                triggered = wl_mod.is_triggered(p, ref) if ref is not None else False
                rows.append({
                    "代码": p["ts_code"],
                    "名称": p.get("name", ""),
                    "昨收": cur,
                    "实时": rt,
                    "进场": entry_p,
                    "盈亏%": pnl,
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
            with st.expander("管理持仓"):
                rm_col1, rm_col2 = st.columns([3, 1])
                pos_options = [f"{p['ts_code']} {p.get('name','')}" for p in wl_data["active_positions"]]
                rm_target = rm_col1.selectbox("归档持仓（移到 archived）", [""] + pos_options, key="rm_pos_sel")
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
                    "昨收": cur,
                    "实时": rt,
                    "触发价": c.get("trigger_price"),
                    "方向": c.get("trigger_direction"),
                    "触发": "🚨" if triggered else "",
                    "备注": c.get("note", ""),
                    "到期": c.get("expires_at", ""),
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
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
                    with st.form("promote_form"):
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
                        promote_submitted = st.form_submit_button("晋升为持仓")
                        if promote_submitted:
                            ts = cand["ts_code"]
                            promote_args = dict(
                                ts_code=ts,
                                entry_price=promote_entry,
                                stop_loss=promote_stop,
                                target=promote_target,
                                expires_days=promote_expires,
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
                                    ),
                                    "from_candidate": True,
                                }
                                st.rerun()
                            except ValueError as e:
                                st.error(str(e))
        else:
            st.info("暂无候选")

    with col_right:
        st.subheader("添加")
        add_type = st.radio("类型", ["持仓", "候选"], horizontal=True)

        with st.form("add_form"):
            ts_code_input = st.text_input("股票代码", placeholder="002050.SZ")
            name_input = st.text_input("名称（可选）")

            if add_type == "持仓":
                c1, c2 = st.columns(2)
                entry_input = c1.number_input("进场价", min_value=0.0, step=0.1)
                stop_input = c2.number_input("止损价", min_value=0.0, step=0.1)
                c3, c4 = st.columns(2)
                target_input = c3.number_input("目标价", min_value=0.0, step=0.1)
                trigger_input = c4.number_input("触发重分析价", min_value=0.0, step=0.1)
                direction_input = st.selectbox("触发方向", ["below", "above"])
                expires_input = st.slider("有效天数", 3, 30, 10)
                submitted = st.form_submit_button("添加持仓")
                if submitted and ts_code_input:
                    code_norm = data.normalize_ts_code(ts_code_input)
                    name_resolved = name_input
                    if not name_resolved:
                        try:
                            info_list = json.loads(data.get_stock_info(ts_code=code_norm))
                            if isinstance(info_list, list) and info_list:
                                name_resolved = info_list[0].get("name", "")
                        except Exception:
                            pass
                    pos_args = dict(
                        ts_code=code_norm, name=name_resolved,
                        entry_price=entry_input, stop_loss=stop_input,
                        target=target_input,
                        trigger_price=trigger_input or None,
                        trigger_direction=direction_input,
                        expires_days=expires_input,
                    )
                    try:
                        wl_mod.add_position(**pos_args)
                        st.session_state.pop("prices", None)
                        st.success(f"✓ 已添加 {code_norm} {name_resolved}")
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
                trigger_input = c1.number_input("触发价", min_value=0.0, step=0.1)
                direction_input = c2.selectbox("触发方向", ["above", "below"])
                note_input = st.text_input("备注")
                expires_input = st.slider("有效天数", 3, 14, 7)
                submitted = st.form_submit_button("添加候选")
                if submitted and ts_code_input:
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
                        direction_input, note_input, expires_input,
                    )
                    st.session_state.pop("prices", None)
                    st.success(f"✓ 已添加候选 {code_norm} {name_resolved}")
                    st.rerun()


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

    with col_out:
        if run_btn and ts_code_analyze:
            code = ts_code_analyze
            progress_msgs = []

            with st.status(f"分析 {code} 中...", expanded=True) as status:
                def on_progress(msg):
                    st.write(msg)
                    progress_msgs.append(msg)

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
                color = VERDICT_COLOR.get(verdict, "gray")

                st.markdown(f"## {verdict_badge(verdict)}  置信度 {conf}/10")

                pa = result.get("price_advice") or {}
                if any(pa.values()):
                    m1, m2, m3 = st.columns(3)
                    m1.metric("建议买入", pa.get("entry", "-"))
                    m2.metric("止损", pa.get("stop_loss", "-"))
                    m3.metric("目标价", pa.get("target", "-"))

                feats = result.get("features") or {}
                if feats:
                    with st.expander("技术特征"):
                        feat_df = pd.DataFrame([feats]).T.rename(columns={0: "值"})
                        feat_df["值"] = feat_df["值"].astype(str)
                        st.dataframe(feat_df, width="stretch")

                if result.get("analysis_text"):
                    with st.expander("完整分析", expanded=True):
                        st.markdown(result["analysis_text"])

        elif "last_analysis" in st.session_state and not run_btn:
            r = st.session_state["last_analysis"]
            if r.get("ts_code") == ts_code_analyze:
                result = r
                pa = result.get("price_advice") or {}
                st.markdown(f"## {verdict_badge(result.get('verdict',''))}  置信度 {result.get('confidence','?')}/10")
                if pa.get("entry"):
                    m1, m2, m3 = st.columns(3)
                    m1.metric("建议买入", pa.get("entry", "-"))
                    m2.metric("止损", pa.get("stop_loss", "-"))
                    m3.metric("目标价", pa.get("target", "-"))
                if result.get("analysis_text"):
                    with st.expander("分析内容", expanded=False):
                        st.markdown(result["analysis_text"])

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

            st.caption("加为候选 = 还没买，等触发；已成交 = 已经买入，直接记为持仓")
            cand_col, pos_col = st.columns(2)

            if cand_col.button("加为候选（等触发）", disabled=not entry_advice):
                name = _resolve_name(r["ts_code"])
                try:
                    wl_mod.add_candidate(
                        r["ts_code"], name,
                        trigger_price=entry_advice,
                        trigger_direction="below",
                        note=f"AI建议买入 {entry_advice}（止损 {stop_advice or '-'}  目标 {target_advice or '-'}）",
                        expires_days=7,
                        stop_advice=stop_advice,
                        target_advice=target_advice,
                    )
                    st.session_state.pop("prices", None)
                    st.success(f"✓ 已加为候选 {r['ts_code']} {name}（触发价 {entry_advice}）")
                except Exception as e:
                    st.error(f"添加失败: {e}")

            if pos_col.button("已成交，记为持仓"):
                name = _resolve_name(r["ts_code"])
                pos_args = dict(
                    ts_code=r["ts_code"], name=name,
                    entry_price=entry_advice,
                    stop_loss=stop_advice,
                    target=target_advice,
                    expires_days=10,
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


# ── Tab 3: Backtest ─────────────────────────────────────────────────────────���─
with tab_bt:
    st.subheader("历史判断回测")
    st.caption(f"读取 ~/.stock-journal/ 中的多头信号，模拟 N 天后 P&L")

    col_ctrl, col_info = st.columns([1, 3])
    with col_ctrl:
        bt_code = st.text_input("只看某只股票（留空=全部）", key="bt_code")
        bt_days = st.slider("持仓天数", 3, 30, 10)
        run_bt = st.button("▶ 运行回测", type="primary")

    if run_bt:
        with st.spinner("回测中..."):
            from apex import backtest as bt_mod
            df = bt_mod.run(ts_code=data.normalize_ts_code(bt_code) or None, lookforward_days=bt_days)
            st.session_state["bt_df"] = df

    df = st.session_state.get("bt_df")
    if df is not None and not df.empty:
        # Summary metrics
        st.markdown("---")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("多头信号数", len(df))
        m2.metric("胜率", f"{df['hit'].mean():.1%}")
        m3.metric("平均收益", f"{df['total_return'].mean():.2%}")
        if df["max_drawdown"].notna().any():
            m4.metric("平均最大回撤", f"{df['max_drawdown'].mean():.2%}")

        # Return distribution bar chart
        df_plot = df.copy()
        df_plot["label"] = df_plot["ts_code"] + " " + df_plot["date"].str[5:]
        df_plot["color"] = df_plot["hit"].map({True: "盈利", False: "亏损"})
        df_plot["return_pct"] = (df_plot["total_return"] * 100).round(2)

        fig = px.bar(
            df_plot.sort_values("total_return"),
            x="return_pct", y="label",
            color="color",
            color_discrete_map={"盈利": "#26a641", "亏损": "#d73a49"},
            orientation="h",
            labels={"return_pct": "收益率 (%)", "label": ""},
            title=f"各信号收益率（持仓 {bt_days} 天）",
            height=max(300, len(df) * 28),
        )
        fig.update_layout(showlegend=True, margin=dict(l=120))
        st.plotly_chart(fig, use_container_width=True)

        # Per-stock summary
        if len(df["ts_code"].unique()) > 1:
            per_stock = df.groupby("ts_code").agg(
                信号数=("hit", "count"),
                胜率=("hit", "mean"),
                平均收益=("total_return", "mean"),
            ).reset_index()
            per_stock["胜率"] = per_stock["胜率"].map("{:.0%}".format)
            per_stock["平均收益"] = per_stock["平均收益"].map("{:.2%}".format)
            st.dataframe(per_stock, width="stretch", hide_index=True)

        # Detail table
        with st.expander("详细数据"):
            detail = df.copy()
            detail["total_return"] = (detail["total_return"] * 100).round(2).astype(str) + "%"
            if detail["max_drawdown"].notna().any():
                detail["max_drawdown"] = (detail["max_drawdown"] * 100).round(2).astype(str) + "%"
            st.dataframe(detail, width="stretch", hide_index=True)

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
