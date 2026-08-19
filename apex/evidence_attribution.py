"""Evidence 归因 —— 把 journal[].evidence 里每条「数据点 → 推论」按预定义
pattern 分类，统计 AI 引用论据的习惯画像，并在样本足够时叠加实际 P&L 胜率。

设计：
  Phase A（立刻有用）：按 verdict × pattern 统计引用频次，反映 AI 的论据偏好。
  Phase B（自动激活）：当 (journal entry, closed position) 匹配数 ≥ n_min 时，
    输出每个 pattern 的实际 win_rate / avg_pnl，反向告诉 AI 哪些论据系统性输钱。

公共 API：
  compute()                  —— 重算并落盘 ~/.stock-journal/evidence_attribution.json
  load()                     —— 读最近一份结果
  format_for_prompt(n_min)   —— markdown 表格，准备塞 system prompt
  inject_into(base)          —— 追加到 base prompt（无数据则返回原文）

匹配规则：
  - 一条 evidence 可命中多个 pattern（不互斥；MACD 金叉 + 放量同时计入两桶）
  - 关联 outcome：同 ts_code，position.entry_date ∈ [journal.date, journal.date+14d]，
    多候选取 entry_date 最近的
  - 胜判：realized_pnl_pct > +0.5%；败：< -0.5%；中间算 breakeven
"""
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apex import config, journal

_TZ_CN = timezone(timedelta(hours=8))

_WIN_THRESHOLD = 0.005
_LOSS_THRESHOLD = -0.005
_MATCH_WINDOW_DAYS = 14


# ── Pattern taxonomy ─────────────────────────────────────────────────────────
# tag → 中文展示名, 正则
# 经过对真实 evidence 样本的对照（5月14日 9 条 evidence）调整：AI 习惯写中英混合，
# 类似 "close=12.83 < MA5=12.88" / "vol_ratio=0.93" / "PE_TTM=69.84"。
_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    # ── 均线 ─────────────────────────────────────────
    ("ma_breakout_up", "均线上穿/突破",
     re.compile(r"(上穿|突破|站上|站稳)\s*MA\s*\d+|close\s*[=＝]\s*[\d.]+\s*>\s*MA\s*\d+", re.I)),
    ("ma_breakdown", "均线跌破/失守",
     re.compile(r"(跌破|下穿|失守)\s*MA\s*\d+|close\s*[=＝]\s*[\d.]+\s*<\s*MA\s*\d+", re.I)),
    ("ma_bull_align", "均线多头排列",
     re.compile(r"多头排列|均线\s*多头|MA\s*多头|bullish.{0,8}alignment", re.I)),
    ("ma_bear_align", "均线空头排列",
     re.compile(r"空头排列|均线\s*空头|MA\s*空头|bearish.{0,8}alignment", re.I)),

    # ── 量能 ─────────────────────────────────────────
    ("vol_surge", "放量",
     re.compile(r"放量|量\s*能\s*放大|vol_ratio\s*[=＝]\s*[1-9]\.\d|量比\s*[=＝]?\s*[1-9]\.\d", re.I)),
    ("vol_shrink", "缩量",
     re.compile(r"缩量|量\s*能\s*萎缩|vol_ratio\s*[=＝]\s*0\.[0-8]\d?|量比\s*[=＝]?\s*0\.[0-8]", re.I)),

    # ── 动量指标 ─────────────────────────────────────
    ("rsi_overbought", "RSI 超买",
     re.compile(r"RSI[^<>]{0,12}(超买|>\s*70|=\s*[7-9]\d|接近\s*[7-9]\d)", re.I)),
    ("rsi_oversold", "RSI 超卖",
     re.compile(r"RSI[^<>]{0,12}(超卖|<\s*30|=\s*[12]\d|接近\s*[12]\d)", re.I)),
    ("macd_golden", "MACD 金叉",
     re.compile(r"MACD.{0,10}金叉|MACD.{0,10}上穿|DIF.{0,10}上穿", re.I)),
    ("macd_dead", "MACD 死叉",
     re.compile(r"MACD.{0,10}死叉|MACD.{0,10}下穿|DIF.{0,10}下穿", re.I)),
    ("macd_above_zero", "MACD 零轴上",
     re.compile(r"MACD.{0,10}(零轴.{0,6}上|>\s*0|位于零轴)", re.I)),
    ("macd_below_zero", "MACD 零轴下",
     re.compile(r"MACD.{0,10}(零轴.{0,6}下|<\s*0)", re.I)),

    # ── 价格行为 ─────────────────────────────────────
    ("price_new_high", "创新高/突破前高",
     re.compile(r"创新高|新高|突破\s*前高|历史新高")),
    ("price_new_low", "创新低/跌破前低",
     re.compile(r"创新低|新低|跌破\s*前低|历史新低")),
    ("divergence", "技术背离",
     re.compile(r"背离|顶背离|底背离")),

    # ── 基本面 ───────────────────────────────────────
    ("earnings_beat", "业绩超预期/增长",
     re.compile(
         r"业绩\s*(超|大超|超出)\s*预期|净利润[^下]{0,8}增长|"
         r"(净利润|归母净利润|利润)[^→\n]{0,20}同比\s*[+]\s*\d|"
         r"业绩\s*亮眼|盈利能力\s*提升")),
    ("earnings_miss", "业绩不及/下滑/亏损",
     re.compile(
         r"业绩\s*(不及|低于|远低于)\s*预期|"
         r"净利润.{0,6}(下滑|下降|减少|负增长)|"
         r"(净利润|归母净利润|利润)[^→\n]{0,20}同比\s*[-−]\s*\d|"
         r"亏损|预亏|Q\d.{0,4}转亏|断崖")),
    ("revenue_growth", "营收增长",
     re.compile(r"营收[^→\n]{0,20}(同比\s*[+]\s*\d|增长|连续.{0,6}增长)")),
    ("revenue_decline", "营收下滑",
     re.compile(r"营收[^→\n]{0,20}(同比\s*[-−]\s*\d|下滑|降\s*\d|减少|萎缩)")),
    ("profit_quality_low", "盈利质量差",
     re.compile(r"扣非.{0,30}(低|仅|占|差|微)|利润率\s*仅|经营\s*现金流.{0,8}(差|为负)")),
    ("valuation_high", "估值偏高",
     re.compile(
         r"估值\s*(偏高|过高|高估)|"
         r"PE[\s_\w]*\s*\(?TTM?\)?\s*[=＝:：]?\s*\d{2,}|"
         r"PE[\s_\w]*\(?\)?\s*[=＝:：]?\s*[5-9]\d|"
         r"PE.{0,8}(偏高|过高|高于)|超越.{0,4}目标价")),
    ("valuation_low", "估值偏低/合理",
     re.compile(r"估值\s*(偏低|低估|合理|具有吸引力)|PE.{0,8}(偏低|低估|合理)")),
    ("high_leverage", "高杠杆/负债",
     re.compile(r"资产负债率\s*[=＝:：]?\s*[6-9]\d|负债\s*(偏高|过高)|杠杆.{0,4}(高|侵蚀)")),
    ("delisting_risk", "退市/ST/持续经营",
     re.compile(r"ST\s*(标签|股|警示)|退市|持续经营.{0,8}(不确定|存疑|风险)|审计.{0,8}保留")),

    # ── 股东/资金/消息 ───────────────────────────────
    ("shareholders_reduce", "减持/解禁",
     re.compile(r"减持|大股东.{0,4}卖|高管.{0,4}减持|限售解禁|股东.{0,4}套现")),
    ("shareholders_add", "增持/回购",
     re.compile(r"(?<!减)增持|大股东.{0,4}买|高管.{0,4}增持|股份回购|公司回购|控股股东.{0,4}增持")),
    ("north_inflow", "北向净流入",
     re.compile(r"北向.{0,6}(净流入|加仓|买入)|沪股通.{0,6}净流入|深股通.{0,6}净流入")),
    ("north_outflow", "北向净流出",
     re.compile(r"北向.{0,6}(净流出|减仓|卖出)|沪股通.{0,6}净流出|深股通.{0,6}净流出")),
    ("main_money_inflow", "主力资金流入",
     re.compile(r"主力.{0,4}资金.{0,4}(净流入|流入|加仓|增仓)|机构.{0,6}买入")),
    ("main_money_outflow", "主力资金流出",
     re.compile(r"主力.{0,4}资金.{0,4}(净流出|流出|减仓|撤退)|机构.{0,6}卖出|机构.{0,6}减仓")),
    ("block_trade_discount", "大宗交易折价",
     re.compile(r"大宗交易.{0,20}折价|大宗.{0,10}折扣|协议转让.{0,10}折价")),
    ("regulatory_risk", "监管风险",
     re.compile(r"立案|处罚|问询函|监管函|诉讼|违规|警示函|被调查")),
    ("dragon_tiger", "龙虎榜/游资",
     re.compile(r"龙虎榜|游资.{0,6}(买入|抢筹)|机构.{0,6}专用|席位")),
    ("research_upgrade", "研报上调",
     re.compile(r"上调.{0,6}评级|上调.{0,6}目标价|买入评级|强烈推荐|首次覆盖.{0,6}买入")),
    ("research_downgrade", "研报下调",
     re.compile(r"下调.{0,6}评级|下调.{0,6}目标价|卖出评级|减持评级")),
    ("corp_action", "定增/重组/并购",
     re.compile(r"定向增发|定增|资产重组|重大重组|并购|收购")),
    ("industry_catalyst", "行业景气/政策利好",
     re.compile(r"行业\s*景气|景气\s*(支撑|向上|提升|加速)|涨价潮?|供需\s*紧?|政策\s*(利好|催化|支持)|交期\s*拉长")),
    ("industry_downturn", "行业景气下行",
     re.compile(r"行业\s*(下行|低迷|景气.{0,4}回落)|景气\s*(下行|低迷)|需求\s*疲软|价格\s*下行")),
    ("good_news_priced_in", "利好出尽/见光死",
     re.compile(r"利好\s*出尽|利好.{0,4}兑现.{0,8}(撤退|回落|下跌)|见光\s*死|预期\s*兑现")),
    ("prior_call_validated", "前次判断已验证",
     re.compile(r"(上次|前次|之前)\s*(判断|看多|看空|偏多|偏空).{0,30}(验证|兑现|正确|印证)")),

    # ── 市场环境 ─────────────────────────────────────
    ("market_weak", "大盘弱势",
     re.compile(r"大盘\s*(弱势|下跌|破位|杀跌)|沪深300.{0,8}(下跌|弱势)|指数.{0,8}弱势")),
    ("market_strong", "大盘强势",
     re.compile(r"大盘\s*(强势|上涨|拉升)|沪深300.{0,8}(上涨|强势)|指数.{0,8}强势")),
    ("sector_strong", "板块强势",
     re.compile(r"板块\s*(强势|领涨|拉升)|行业\s*(强势|领涨)")),
    ("sector_weak", "板块弱势",
     re.compile(r"板块\s*(弱势|下跌|杀跌|领跌)|行业\s*(弱势|领跌)")),
    ("relative_strength", "个股跑赢大盘/板块",
     re.compile(r"跑赢|强于\s*(大盘|板块|行业|指数)|相对强度.{0,4}(高|强)")),
    ("relative_weakness", "个股跑输大盘/板块",
     re.compile(r"跑输|弱于\s*(大盘|板块|行业|指数)|相对强度.{0,4}(低|弱)")),
]


def _path() -> Path:
    cfg = config.get()
    return Path(cfg["paths"]["journal_dir"]).expanduser() / "evidence_attribution.json"


def _classify(text: str) -> list[str]:
    """返回该 evidence 字符串命中的所有 pattern tag。"""
    if isinstance(text, dict):
        text = " ".join(str(text.get(key) or "") for key in ("fact", "inference"))
    if not isinstance(text, str):
        return []
    return [tag for tag, _, pat in _PATTERNS if pat.search(text)]


def _pattern_label(tag: str) -> str:
    for t, label, _ in _PATTERNS:
        if t == tag:
            return label
    return tag


def _link_to_closed(entry: dict, closed_positions: list[dict]) -> Optional[float]:
    """journal entry → 实际 realized_pnl_pct（小数，不是百分比）。无匹配返回 None。"""
    ts_code = entry.get("ts_code")
    j_date_str = entry.get("date") or ""
    if not ts_code or not j_date_str:
        return None
    try:
        j_date = date.fromisoformat(j_date_str)
    except (TypeError, ValueError):
        return None

    candidates: list[tuple[int, float]] = []
    for c in closed_positions:
        if c.get("ts_code") != ts_code:
            continue
        e_date_str = (c.get("open") or {}).get("entry_date") or ""
        if not e_date_str:
            continue
        try:
            e_date = date.fromisoformat(e_date_str)
        except (TypeError, ValueError):
            continue
        delta = (e_date - j_date).days
        if not (0 <= delta <= _MATCH_WINDOW_DAYS):
            continue
        pnl = (c.get("close") or {}).get("realized_pnl_pct")
        if pnl is None:
            continue
        try:
            candidates.append((delta, float(pnl)))
        except (TypeError, ValueError):
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def _empty_bucket() -> dict:
    return {
        "n": 0,
        "by_verdict": defaultdict(int),
        "outcome_n": 0,
        "wins": 0,
        "losses": 0,
        "breakevens": 0,
        "pnl_sum": 0.0,
        "pnl_min": None,
        "pnl_max": None,
    }


def compute() -> dict:
    """重算并写入 evidence_attribution.json。"""
    from apex import watchlist as _wl
    closed = _wl.load_closed_positions()
    entries = journal.load_verdicts()

    by_pattern: dict[str, dict] = defaultdict(_empty_bucket)
    unmatched_samples: list[str] = []

    total_entries = 0
    entries_with_evidence = 0
    total_evidence = 0
    matched_evidence = 0
    entries_with_outcome = 0

    for e in entries:
        total_entries += 1
        evidences = e.get("evidence") or []
        if not evidences:
            continue
        entries_with_evidence += 1

        verdict = str(e.get("verdict") or "unknown")
        pnl = _link_to_closed(e, closed)
        if pnl is not None:
            entries_with_outcome += 1

        for ev in evidences:
            if not isinstance(ev, str) or not ev.strip():
                continue
            total_evidence += 1
            tags = _classify(ev)
            if tags:
                matched_evidence += 1
            elif len(unmatched_samples) < 30:
                unmatched_samples.append(ev[:150])

            for tag in tags:
                b = by_pattern[tag]
                b["n"] += 1
                b["by_verdict"][verdict] += 1
                if pnl is not None:
                    b["outcome_n"] += 1
                    b["pnl_sum"] += pnl
                    b["pnl_min"] = pnl if b["pnl_min"] is None else min(b["pnl_min"], pnl)
                    b["pnl_max"] = pnl if b["pnl_max"] is None else max(b["pnl_max"], pnl)
                    if pnl > _WIN_THRESHOLD:
                        b["wins"] += 1
                    elif pnl < _LOSS_THRESHOLD:
                        b["losses"] += 1
                    else:
                        b["breakevens"] += 1

    out_patterns: dict[str, dict] = {}
    for tag, b in by_pattern.items():
        on = b["outcome_n"]
        out_patterns[tag] = {
            "label": _pattern_label(tag),
            "n": b["n"],
            "by_verdict": dict(b["by_verdict"]),
            "outcome_n": on,
            "wins": b["wins"],
            "losses": b["losses"],
            "breakevens": b["breakevens"],
            "win_rate": round(b["wins"] / on, 3) if on else None,
            "avg_pnl_pct": round(b["pnl_sum"] / on, 4) if on else None,
            "best_pnl_pct": round(b["pnl_max"], 4) if b["pnl_max"] is not None else None,
            "worst_pnl_pct": round(b["pnl_min"], 4) if b["pnl_min"] is not None else None,
        }

    out = {
        "computed_at": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
        "total_entries": total_entries,
        "entries_with_evidence": entries_with_evidence,
        "entries_with_outcome": entries_with_outcome,
        "total_evidence": total_evidence,
        "matched_evidence": matched_evidence,
        "match_rate": round(matched_evidence / total_evidence, 3) if total_evidence else 0.0,
        "by_pattern": out_patterns,
        "unmatched_samples": unmatched_samples,
    }
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out


def load() -> Optional[dict]:
    p = _path()
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def format_for_prompt(n_min: int = 3,
                      outcome_n_min: int = 5,
                      attribution: Optional[dict] = None) -> str:
    """生成 markdown，可直接拼到 system prompt。

    n_min：pattern 进入"引用偏好"表的最低出现次数。
    outcome_n_min：pattern 显示 win_rate 的最低已结仓样本数。
    无数据返回 ""。
    """
    if attribution is None:
        attribution = load()
    if not attribution:
        return ""

    patterns: dict[str, dict] = attribution.get("by_pattern") or {}
    if not patterns:
        return ""

    eligible = [(tag, b) for tag, b in patterns.items() if (b.get("n") or 0) >= n_min]
    if not eligible:
        return ""

    eligible.sort(key=lambda kv: -(kv[1].get("n") or 0))

    entries_with_ev = attribution.get("entries_with_evidence", 0)
    entries_with_outcome = attribution.get("entries_with_outcome", 0)

    lines = [
        f"## 你的 evidence 论据偏好（基于 {entries_with_ev} 条 evidence 记录）",
        "",
        "_pattern = AI 历史论据按关键词分类聚合；引用占比反映你对该信号的依赖程度。_",
        "",
    ]

    # ── Phase A: 引用频次 + verdict 切片 ─────────────────────────
    lines.append("### 论据使用习惯")
    lines.append("| pattern | 引用次数 | verdict 分布 |")
    lines.append("|---|---|---|")
    for tag, b in eligible:
        verdict_str = ", ".join(
            f"{v}×{c}" for v, c in
            sorted((b.get("by_verdict") or {}).items(), key=lambda kv: -kv[1])
        ) or "-"
        lines.append(f"| {b.get('label', tag)} | {b['n']} | {verdict_str} |")

    # ── Phase B: P&L 校准（样本够才显示）─────────────────────────
    outcome_rows = [
        (tag, b) for tag, b in eligible
        if (b.get("outcome_n") or 0) >= outcome_n_min
    ]
    if outcome_rows:
        lines.append("")
        lines.append(f"### 论据 → 实际 P&L 校准（已结仓样本 ≥ {outcome_n_min}）")
        lines.append("| pattern | 样本 | 胜率 | 平均 P&L | 最差单笔 |")
        lines.append("|---|---|---|---|---|")
        outcome_rows.sort(key=lambda kv: (kv[1].get("win_rate") or 0))
        for tag, b in outcome_rows:
            wr = b.get("win_rate")
            avg = b.get("avg_pnl_pct")
            worst = b.get("worst_pnl_pct")
            lines.append(
                f"| {b.get('label', tag)} | {b['outcome_n']} | "
                f"{wr * 100:.0f}% | {avg * 100:+.2f}% | "
                f"{worst * 100:+.2f}% |"
            )
        lines.append("")
        lines.append(
            "**怎么用**：胜率显著 < 50% 的 pattern 是你的"
            "**系统性误用论据**——本次若仍引用该 pattern，置信度需主动 −1；"
            "若多个低胜率 pattern 同时出现，累加扣减。"
        )
    else:
        lines.append("")
        lines.append(
            f"### 论据 → 实际 P&L 校准"
            f"\n_当前已结仓样本不足（需每 pattern ≥ {outcome_n_min} 笔），"
            f"暂不显示胜率列。继续积累后会自动出现。_"
        )

    lines.append("")
    lines.append(
        "**自我审视**：若本次结论的关键 evidence 全部集中在表格前 2-3 个高频 pattern，"
        "说明你正在依赖惯性论据 —— 主动反问「这次有没有不同的论据角度？」"
    )
    return "\n".join(lines)


def inject_into(base_prompt: str, n_min: int = 3, outcome_n_min: int = 5) -> str:
    """把 evidence attribution 块注入 base_prompt 末尾。
    支持 {{EVIDENCE_ATTRIBUTION}} 占位符；否则追加。无数据时返回原文。
    """
    block = format_for_prompt(n_min=n_min, outcome_n_min=outcome_n_min)
    placeholder = "{{EVIDENCE_ATTRIBUTION}}"
    if not block:
        return base_prompt.replace(placeholder, "")
    if placeholder in base_prompt:
        return base_prompt.replace(placeholder, block)
    return f"{base_prompt.rstrip()}\n\n{block}\n"
