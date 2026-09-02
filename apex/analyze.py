"""
DeepSeek API (OpenAI-compatible) agent for stock analysis.
The AI autonomously calls data tools, then records verdict via record_verdict tool.
"""
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from openai import OpenAI

_TZ_CN = timezone(timedelta(hours=8))

from apex import config as _cfg_mod, data, journal, calibration, evidence_attribution, trace as trace_mod, skills, playstyle, trade_signal, observability, forecast_calibration
from apex.decision_policy import EvidenceClaimError, apply_accuracy_policy
from apex.stock_profile import StockProfile, classify_stock_profile
from apex.journal_views import history_digest
from apex.schemas import (
    VERDICT_ENUM, BULLISH_VERDICTS, BEARISH_VERDICTS,
    STOCK_TYPE_ENUM, VALUATION_BASIS_ENUM, PLAYSTYLE_ENUM,
    POSITION_ACTION_SOURCE,
)
from apex.analysis_graph import GraphHandlers, build_analysis_graph
from apex.evidence import classify_evidence_type, entity_matches, make_evidence_item, source_tier
from apex.evidence_control import EvidenceController
from apex.position_action_state import (
    adapt_legacy_proposal,
    materialize_effective_position_plan,
    validate_proposal_shape,
)
from apex.report_context import build_report_context


class AnalysisError(Exception):
    pass


# ── Tool definitions (OpenAI function-calling format) ────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_daily_price",
            "description": "获取A股日K线数据（含MA5/MA10/MA20/MA60/量比）。返回最近60根K线的JSON。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码，如 002050.SZ"},
                    "start_date": {"type": "string", "description": "开始日期 YYYYMMDD，默认120天前"},
                    "end_date": {"type": "string", "description": "结束日期 YYYYMMDD，默认今天"},
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fundamentals",
            "description": (
                "获取股票深度基本面数据，包含两部分：\n"
                "1) 估值面：PE、PE_TTM、PB、PS_TTM、股息率、换手率、流通市值（来自 daily_basic）\n"
                "2) 财务面：最近4个季度的盈利能力(ROE/ROA/毛利率/净利率/ROIC)、"
                "每股指标(EPS/BPS/经营现金流每股)、偿债能力(资产负债率/流动比率/速动比率)、"
                "同比增长(营收YoY/净利润YoY/ROE YoY)、现金流(FCFF/FCFE)、"
                "以及 Python 预计算的趋势判断和风险标记(summary.flags)。\n"
                "\n"
                "返回结构：valuation(估值) + quarters(最近4季财务) + summary(趋势+风险标记)。\n"
                "summary.flags 是 Python 预计算的客观标记（如 debt_to_assets>70%、ROE连续4季为负），"
                "AI 必须逐条引用，不要自己重新判断这些基础指标。\n"
                "\n"
                "财务数据来自 tushare fina_indicator，若权限不足或数据缺失则 quarters 为空数组、"
                "summary 为 null，此时仅返回 valuation 部分（与旧版行为兼容）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码"},
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_info",
            "description": "获取公司名称、行业、上市日期。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string"},
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "用博查按证据缺口搜索股票相关信息。每次调用聚焦一个 category；已有结构化证据足够时无需调用。\n"
                "常用类别：\n"
                "  · earnings           — 业绩面（季报/预告/营收/净利润），oneMonth 窗口\n"
                "  · shareholders       — 股东动态（减持/增持/解禁/大宗交易），oneMonth 窗口\n"
                "  · regulatory         — 监管/合规（立案/处罚/诉讼/问询函），oneYear 窗口\n"
                "  · money_flow         — 资金面（北向/龙虎榜/主力/机构），oneWeek 窗口\n"
                "\n"
                "其他按需类别：\n"
                "  · corporate_actions  — 资本运作（定增/回购/重组/并购），oneYear 窗口\n"
                "  · research           — 卖方研报（评级/目标价变化），oneMonth 窗口\n"
                "  · industry           — 行业政策（需先用 get_stock_info 拿到 industry 后传入），oneYear 窗口\n"
                "  · general            — 兜底自定义 query\n"
                "\n"
                "返回结果已按信任度排序：cninfo/sse/szse > 东财/同花顺/雪球 > 其他自媒体。\n"
                "高信任来源（官方公告）的权重应明显高于自媒体。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码，如 002050.SZ"},
                    "category": {
                        "type": "string",
                        "enum": [
                            "earnings", "shareholders", "regulatory", "money_flow",
                            "corporate_actions", "research", "industry", "general",
                        ],
                        "description": "搜索类别（必填）。一次调用只能选一个。",
                    },
                    "name": {
                        "type": "string",
                        "description": "公司名。除 industry/general 外强烈建议传入，否则只能用代码召回，质量差。",
                    },
                    "industry": {
                        "type": "string",
                        "description": "行业名。仅当 category=industry 时使用（必填）。",
                    },
                    "query": {
                        "type": "string",
                        "description": "自定义搜索词。仅当 category=general 时生效。",
                    },
                    "freshness": {
                        "type": "string",
                        "enum": ["oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"],
                        "description": "可选；通常用 category 内置默认值，特殊场景才覆盖。",
                    },
                    "count": {"type": "integer", "description": "返回条数，默认 10"},
                },
                "required": ["ts_code", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dragon_tiger_list",
            "description": (
                "查个股近 N 天龙虎榜上榜情况（akshare 东财源，免费）。\n"
                "返回上榜日期清单 + 上榜频次。可选拉最近 3 次的买卖席位 TOP5（机构/游资）。\n"
                "\n"
                "**何时调用**：当 web_search(money_flow) 召回里出现「龙虎榜」字样、"
                "或股价短期异动需要确认是否游资炒作时调用。"
                "比 web_search 召回更结构化、可直接引用具体上榜次数和净买额。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码，如 002050.SZ"},
                    "days": {
                        "type": "integer",
                        "description": "回溯天数（自然日），默认 90",
                    },
                    "fetch_seats": {
                        "type": "boolean",
                        "description": "是否拉最近 3 次的买卖席位 TOP5（默认 false，需要时再开）",
                    },
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_unlock_schedule",
            "description": (
                "查个股限售解禁日程（akshare 东财源，免费）。\n"
                "返回未来 180 天待解禁明细 + 近 180 天已发生解禁（含解禁后 20 日表现）。\n"
                "\n"
                "**何时调用**：分析多头判断前必查。短期内大额解禁（占流通盘 > 5%）是"
                "重要利空信号。比 web_search(shareholders) 召回的「公告减持」更精确、可直接"
                "引用解禁日期 / 股份数 / 占流通比例。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string", "description": "股票代码，如 002050.SZ"},
                    "days_ahead": {
                        "type": "integer",
                        "description": "未来回看天数，默认 180",
                    },
                    "history_days": {
                        "type": "integer",
                        "description": "历史回看天数（评估以往解禁后股价反应），默认 180",
                    },
                },
                "required": ["ts_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mx_data_query",
            "description": (
                "妙想金融数据查询（东方财富官方数据源）。支持自然语言查询行情、财务、"
                "股东、板块、指数等数据。\n"
                "示例：\"贵州茅台近三年净利润 营业收入\" \"东方财富最新价 主力资金流向\"\n"
                "\n"
                "get_fundamentals 已提供结构化的最近4季财务数据（ROE/毛利率/营收增速/现金流等），"
                "mx_data_query 作为补充：当需要查更长历史（如近3年营收趋势）或特定指标（如研发费用）时使用。"
                "相比 get_daily_price：支持更灵活的自然语言查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {
                        "type": "string",
                        "description": "自然语言查询问句，如 \"贵州茅台近三年净利润 营业收入\"",
                    },
                },
                "required": ["query_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mx_news_search",
            "description": (
                "妙想财经资讯搜索（东方财富数据源）。搜索新闻、研报、公告。\n"
                "示例：\"格力电器最新研报\" \"宁德时代利空\"\n"
                "\n"
                "相比 web_search：专注财经领域（东财数据库），召回更垂直精准；"
                "但覆盖广度不如 Bocha（无监管/行业政策等通用搜索）。"
                "两者可互补：如需查财经新闻用 mx_news_search，如需查监管/政策用 web_search。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索问句，如 \"格力电器最新研报\"",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mx_stock_screen",
            "description": (
                "妙想智能选股。通过自然语言筛选A股。\n"
                "示例：\"市盈率低于20且ROE大于15%的A股\" "
                "\"今天放量大涨的股票\" \"北向资金近期增持的股票\"\n"
                "\n"
                "适用于批量筛选候选标的（从几千只股票中缩小范围），"
                "然后可进一步对具体标的调 get_daily_price / 分析等做详细判断。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "自然语言选股条件",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_verdict",
            "description": (
                "记录最终分析结论。分析完成后必须调用此工具，不得省略。\n"
                "**evidence_claims 为必填**：每边 0-3 条，必须引用 evidence_id；不得凑数。"
                "evidence 是兼容展示字段，不参与后端裁决。\n"
                "**entry / stop_loss / target**：仅看多/偏多且当前可执行时填具体价位，"
                "非看多方向（中性及以下）统一填 0。entry 同时给一个成交区间 entry_low/entry_high "
                "（回踩入场：entry_low=区间下沿地板 / entry_high=entry 主锚点；突破入场：entry_low=entry 主锚点 / "
                "entry_high=区间上沿天花板），价格进入此带才触发，避免接飞刀或追高。区间宽度参考 ATR(14)%，"
                "通常 1-3%。非看多方向 entry_low/entry_high 留空不填。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": VERDICT_ENUM,
                        "description": "判断方向",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "兼容字段；后端会将其视为模型原始置信度并执行一次确定性校准",
                    },
                    "model_confidence": {
                        "type": "integer", "minimum": 1, "maximum": 10,
                        "description": "模型基于证据给出的原始置信度；不得手工应用历史加减分",
                    },
                    "proposed_trade_action": {
                        "type": "string",
                        "enum": ["buy", "watch", "avoid"],
                        "description": "交易意图。公司值得跟踪但当前没有合格买点时必须填 watch，不能用 buy 代替长期看好。",
                    },
                    "entry_style": {
                        "type": "string",
                        "enum": ["pullback", "breakout"],
                        "description": "入场方式：pullback=等待回踩区间，breakout=等待向上突破区间。",
                    },
                    "valid_for_days": {
                        "type": "integer", "minimum": 1, "maximum": 10,
                        "description": "买入区间从下一交易日起有效的交易日数，默认建议 3。",
                    },
                    "entry": {"type": "number", "description": "建议买入价（主锚点）。看多/偏多且可执行时填具体数字，其他方向填 0。"},
                    "entry_low": {"type": "number", "description": "买入区间下沿（地板价）。回踩入场=下方支撑，突破入场=entry 主锚点。看多类必填，非看多方向不填。"},
                    "entry_high": {"type": "number", "description": "买入区间上沿（天花板）。回踩入场=entry 主锚点，突破入场=上方阻力。看多类必填，非看多方向不填。"},
                    "stop_loss": {"type": "number", "description": "止损价。看多/偏多且可执行时填具体数字，其他方向填 0。"},
                    "target": {"type": "number", "description": "目标价。看多/偏多且可执行时填具体数字，其他方向填 0。"},
                    "features": {
                        "type": "object",
                        "description": "技术特征快照",
                        "properties": {
                            "ma5_position": {"type": "string", "enum": ["above", "below"]},
                            "ma20_position": {"type": "string", "enum": ["above", "below"]},
                            "ma_alignment": {"type": "string", "enum": ["bullish", "bearish", "mixed"]},
                            "volume_ratio": {"type": "number"},
                            "macd_zone": {"type": "string", "enum": ["above_zero", "below_zero"]},
                            "rsi_14": {"type": "number"},
                            "price_vs_ma5_pct": {"type": "number"},
                            "atr_14_pct": {"type": "number", "description": "ATR(14)占最新收盘价的百分比，用于止损宽度计算"},
                            "candle_direction": {"type": "string", "enum": ["阳", "阴", "十字星"], "description": "最近一根 K 线的实体方向"},
                            "candle_body_pct": {"type": "number", "description": "实体占当日振幅百分比"},
                            "candle_upper_shadow_pct": {"type": "number", "description": "上影线占当日振幅百分比"},
                            "candle_lower_shadow_pct": {"type": "number", "description": "下影线占当日振幅百分比"},
                            "candle_pattern": {"type": "string", "description": "识别到的蜡烛形态标签，如 长上影/长下影/锤子线/射击之星/双顶雏形/量价背离/无"},
                        },
                        "required": ["ma5_position", "ma20_position", "volume_ratio", "rsi_14", "atr_14_pct"],
                    },
                    "evidence": {
                        "type": "array",
                        "description": (
                            "兼容展示用证据文本；后端只使用 evidence_claims 裁决。"
                        ),
                        "items": {"type": "string"},
                        "minItems": 0,
                    },
                    "evidence_claims": {
                        "type": "array",
                        "description": "引用证据账本的结构化多空证据；每边最多 3 条，允许某一边为 0 条。",
                        "maxItems": 6,
                        "items": {
                            "type": "object",
                            "properties": {
                                "evidence_id": {"type": "string"},
                                "stance": {"type": "string", "enum": ["bull", "bear"]},
                                "dimension": {"type": "string", "enum": ["technical", "fundamental", "capital", "sentiment"]},
                                "nature": {"type": "string", "enum": ["fact", "current", "forecast"]},
                                "hardness": {"type": "integer", "minimum": 1, "maximum": 5},
                                "as_of": {"type": "string", "description": "证据时点 YYYY-MM-DD"},
                                "frequency": {"type": "string", "enum": ["intraday", "daily", "weekly", "monthly", "quarterly", "event"]},
                                "is_complete": {"type": "boolean"},
                                "independence_group": {"type": "string"},
                                "inference": {"type": "string"},
                            },
                            "required": [
                                "evidence_id", "stance", "dimension", "nature", "hardness",
                                "as_of", "frequency", "is_complete", "independence_group", "inference",
                            ],
                        },
                    },
                    "position_size_pct": {
                        "type": "integer",
                        "description": (
                            "建议仓位占账户总资金的百分比（0-50）。"
                            "映射：confidence 1-2→0%, 3→≤5%, 4→≤10%, 5→≤15%, "
                            "6→≤20%, 7→≤30%, 8→≤35%, 9→≤40%, 10→≤50%。"
                            "观望偏多/中性/观望偏空必须为 0，且不得授权交易。"
                        ),
                    },
                    "new_info": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "本次分析相比上次的新增信息（事件/数据/形态变化），无则填空数组。"
                            "24h 内重复分析时，系统据此判断是否豁免限幅："
                            "new_info 非空且客观技术面确有变化才不限幅，否则方向限 ±1 档、conf 限 ±2。"
                        ),
                    },
                    "setup_tag": {
                        "type": "string",
                        "description": (
                            "本次分析对应的交易原型（setup），用于「我的交易系统」层聚类与未来 Rule Discovery。"
                            "从种子词表选一：打板/首板/龙回头/板块轮动/超跌反弹/趋势突破/业绩驱动/题材炒作/低位反转；"
                            "都不贴切则填「其他:<自定义>」。基于本次分析最核心的驱动逻辑判断，非看多方向也填（描述若入场的话是什么 setup）。"
                        ),
                    },
                    "stock_type": {
                        "type": "string",
                        "enum": STOCK_TYPE_ENUM,
                        "description": (
                            "兼容审计字段；最终股票类型由后端软分类覆盖，模型无需据此裁决。"
                        ),
                    },
                    "valuation_basis": {
                        "type": "string",
                        "enum": VALUATION_BASIS_ENUM,
                        "description": (
                            "仅偏空类（看空/偏空/观望偏空）必填，其他方向不填。本次空头结论中「估值」是不是主要依据："
                            "forward_valuation=基于 Forward PE/PEG/一致预期等前瞻估值（成长股偏空唯一允许的估值依据）；"
                            "static_pe_only=仅静态 PE_TTM（成长股偏空会被拒）；"
                            "non_valuation=估值非主要依据。"
                        ),
                    },
                    "playstyle": {
                        "type": "object",
                        "description": (
                            "标的玩法判定（打野/波段/中线/长线）。基于注入的 ## 玩法特征(Playstyle FE) + web_search 定性判断。"
                            "FE 完整度 ≥50% 时必填，<50% 允许 null。玩法=股票客观属性，非当前趋势（见 Skill: playstyle）。"
                            "primary 必须 = ratings 的并列最高（ratings[primary]==max）；secondary=次优或兼容玩法，无则留空。"
                            "reasons 每条必须引用 FE 特征数字 或 web_search 结果，禁凭空编。"
                        ),
                        "properties": {
                            "ratings": {
                                "type": "object",
                                "description": "4 档玩法 0-5 星级",
                                "properties": {
                                    "打野": {"type": "integer"},
                                    "波段": {"type": "integer"},
                                    "中线": {"type": "integer"},
                                    "长线": {"type": "integer"},
                                },
                            },
                            "primary": {"type": "string", "enum": PLAYSTYLE_ENUM, "description": "主玩法 = ratings 并列最高"},
                            "secondary": {"type": "string", "enum": PLAYSTYLE_ENUM, "description": "兼容玩法（次优/可替代），无则不填"},
                            "reasons": {"type": "array", "items": {"type": "string"}, "description": "原因，引用 FE 数字或 web_search 结果"},
                        },
                        "required": ["ratings", "primary", "reasons"],
                    },
                },
                "required": [
                    "verdict", "model_confidence", "proposed_trade_action", "entry_style",
                    "valid_for_days", "entry", "stop_loss", "target", "features", "evidence_claims",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_position_action",
            "description": (
                "记录已持仓票的加减仓建议。重新分析一只**你已持有**的票时调此工具；未持仓的票调 record_verdict（调错会被系统拒绝）。"
                "action=add 必填 add_shares(>0)；action=trim 二选一 trim_shares 或 trim_pct(0-1]；action=exit/hold 可只给 new_stop。"
                "ladder_intent 必填且只能是 preserve、replace 或 clear：preserve 保留现有 ladder 且不得提交 scale_plan；"
                "replace 必须提交完整非空 scale_plan；clear 明确清空现有 ladder 且不得提交 scale_plan。"
                "系统会按价格顺序模拟执行 ladder 并拒绝不自洽路径：下行路径（现价往下）必须单一意图——纯回踩加仓或纯防守减仓，先卖后买/先买后卖是 churn（两档若互斥请合并为单一防守档或拉开到不同情景）；上行路径先加后减（金字塔），trim 之后不得再有 add；trim 低于有效止损（含路径内止损上移后的新止损）是死档；add ≥ 有效止盈价是自相矛盾（用 new_target 上移止盈修复）。"
                "target 是建仓时的一次性字段，价格观上移时必须用 new_target 同步止盈，否则化石止盈（monitor 推送）会与 ladder 打架。"
                "rationale 是机器可读摘要，完整推理写进分析文本。"
                "action=trim/exit 涉及实质风险决策；只有证据控制器确认重大风险已核实、独立复核通过后才会被接受。"
                "4h 内重复调此工具会被反 churn 速率限制拒绝，除非 new_info 列出本次新增信息。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["hold", "add", "trim", "exit"], "description": "加减仓动作"},
                    "add_shares": {"type": "integer", "description": "action=add 必填，加仓股数(>0)"},
                    "trim_shares": {"type": "integer", "description": "action=trim 二选一，减仓股数(>0)"},
                    "trim_pct": {"type": "number", "description": "action=trim 二选一，减仓比例 0-1（如 0.33=减1/3）"},
                    "new_stop": {
                        "anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}],
                        "description": "正数止损替换建议；null 表示保持现有止损",
                    },
                    "new_target": {
                        "anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}],
                        "description": "正数止盈替换建议；null 表示保持现有目标",
                    },
                    "ladder_intent": {
                        "type": "string", "enum": ["preserve", "replace", "clear"],
                        "description": "preserve=保留现有 ladder；replace=用完整 scale_plan 替换；clear=明确清空 ladder",
                    },
                    "scale_plan": {
                        "type": "array",
                        "minItems": 1,
                        "description": "仅 ladder_intent=replace 时提交的完整非空 ladder 计划",
                        "items": {
                            "type": "object",
                            "properties": {
                                "level": {"type": "integer", "description": "1=首加/首减, 2=二加..."},
                                "trigger_price": {"type": "number", "exclusiveMinimum": 0, "description": "触发价"},
                                "action": {"type": "string", "enum": ["add", "trim"]},
                                "shares": {"type": "integer", "minimum": 1, "description": "加/减仓股数"},
                                "pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 1, "description": "减仓比例 (0,1]（shares/pct 二选一）"},
                                "new_stop": {"type": "number", "description": "触发后止损；仅 trim pct=1.0 的完整退出档可用非正数，系统会规范化为 null"},
                                "reason": {"type": "string", "description": "该档触发理由"},
                            },
                            "required": ["action", "trigger_price"],
                            "oneOf": [
                                {"required": ["shares"], "not": {"required": ["pct"]}},
                                {"required": ["pct"], "not": {"required": ["shares"]}},
                            ],
                        },
                    },
                    "rationale": {"type": "string", "description": "机器可读摘要（why this action + ladder），完整推理写进分析文本"},
                    "new_info": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "本次相比上次 position_action 的新增信息（4h 内重复时据此豁免反 churn 速率限制）",
                    },
                },
                "required": ["action", "rationale", "ladder_intent"],
                "allOf": [{
                    "if": {"properties": {"ladder_intent": {"const": "replace"}}},
                    "then": {"required": ["scale_plan"]},
                }],
            },
        },
    },
]

TOOLS.insert(-2, {
    "type": "function",
    "function": {
        "name": "submit_research_state",
        "description": (
            "提交当前研究状态，让证据控制器判断继续补证、形成初稿或弃权。"
            "完成基础结构化数据分析后调用；每次获得能解决关键缺口的新证据后再次调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "thesis": {"type": "string", "description": "当前最可能的方向及一句话原因"},
                "strongest_bull_evidence": {"type": "array", "items": {"type": "string"}},
                "strongest_bear_evidence": {"type": "array", "items": {"type": "string"}},
                "gaps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "description": {"type": "string"},
                            "severity": {"type": "string", "enum": ["critical", "noncritical"]},
                            "status": {"type": "string", "enum": ["open", "resolved"]},
                        },
                        "required": ["id", "description", "severity", "status"],
                    },
                },
                "next_actions": {"type": "array", "items": {"type": "string"}},
                "ready": {"type": "boolean", "description": "关键证据是否已收敛"},
            },
            "required": ["thesis", "gaps", "next_actions", "ready"],
        },
    },
})


def _load_system_prompt(*, skill_names: list[str] | None = None) -> str:
    cfg = _cfg_mod.get()
    prompt_path = Path(cfg["paths"]["prompt_file"]).expanduser()
    if prompt_path.exists():
        base = prompt_path.read_text(encoding="utf-8")
    else:
        base = (
            "你是一位资深A股研究员。对给定股票提交可引用的结构化证据和解释；"
            "最终方向、置信度和交易授权由后端策略确定。分析完成后必须调用 record_verdict。"
        )
    # 方向预测校准由 decision-policy-v1 在后端执行一次。旧实盘 calibration
    # 仍供绩效页面使用，但不再注入模型，避免选择偏差和双重扣分。
    base = evidence_attribution.inject_into(base)
    # 回测复盘→分析闭环：注入最近一次全局复盘的 prompt_injection。
    # 时效(max_inject_age_days) + 样本量(min_review_samples) 门槛已由 load 端把关，
    # 不满足则返回 None 不注入，避免旧/小样本结论误导本次分析。
    try:
        from apex import backtest_review
        inj = backtest_review.load_prompt_injection()
        if inj:
            base += "\n\n## 回测复盘提醒（基于历史模拟回测派生，注意样本局限，非定论）\n" + inj
    except Exception:
        pass
    # 只注入软分类实际启用的领域 skill。
    base += skills.load_skills_for("analyze", names=skill_names or [])
    return base


def _dispatch_tool(name: str, tool_input: dict) -> str:
    if name not in data.TOOL_FUNCTIONS:
        raise AnalysisError(f"Unknown tool: {name}")
    result = data.TOOL_FUNCTIONS[name](**tool_input)

    # 为 get_daily_price 返回注入 ATR(14) + 蜡烛图形态，让 AI 直接引用预计算结果
    if name == "get_daily_price":
        try:
            bars = json.loads(result)
            if isinstance(bars, list) and len(bars) >= 15:
                from apex import technical
                atr_val = technical.atr_14(bars)
                atr_pct = technical.atr_14_pct(bars)
                candlestick = data.compute_candlestick_features(bars)
                result = json.dumps({
                    "bars": bars,
                    "atr_14": atr_val,
                    "atr_14_pct": atr_pct,
                    "candlestick": candlestick,
                }, ensure_ascii=False)
        except Exception:
            pass  # 注入失败不影响原始返回

    return result


def _make_client(cfg: dict) -> OpenAI:
    from apex.llm import make_client
    client = make_client(
        api_key=cfg["deepseek"]["api_key"],
        base_url=cfg["deepseek"].get("base_url", "https://api.deepseek.com"),
    )
    return observability.wrap_analysis_client(client)


def _fetch_forward_bars(ts_code: str, start_d: date, end_d: date) -> list[dict]:
    """拉取 [start_d, end_d] 区间的日 K 线（升序）。失败返回 []。

    复用 data.get_daily_price，注意它会 tail(60)——所以窗口超过 60 个交易日时，
    最早的 entries 会拿不到前向收益（acceptable degradation）。
    """
    try:
        raw = data.get_daily_price(
            ts_code,
            start_date=start_d.strftime("%Y%m%d"),
            end_date=end_d.strftime("%Y%m%d"),
        )
        bars = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(bars, list):
            return []
        bars.sort(key=lambda b: str(b.get("trade_date", "")))
        return bars
    except Exception:
        return []


def _forward_return_pct(j_date: date, bars: list[dict],
                        n_trading_days: int = 10,
                        min_trading_days: int = 5) -> Optional[tuple[float, str, int]]:
    """从 j_date 起算最多 N 个交易日的收益率（%）。

    数据不足 N 天时降级到可用窗口，但至少需要 min_trading_days 天，否则返回 None。
    返回 (pct, exit_trade_date, actual_n) 或 None。
    """
    if not bars:
        return None
    j_str = j_date.strftime("%Y%m%d")
    entry_idx = None
    for i, b in enumerate(bars):
        td = str(b.get("trade_date", "")).replace("-", "")
        if td >= j_str:
            entry_idx = i
            break
    if entry_idx is None:
        return None
    available = len(bars) - 1 - entry_idx
    if available < min_trading_days:
        return None
    actual_n = min(n_trading_days, available)
    exit_idx = entry_idx + actual_n
    p0 = bars[entry_idx].get("close")
    p1 = bars[exit_idx].get("close")
    if p0 in (None, 0) or p1 is None:
        return None
    try:
        pct = (float(p1) - float(p0)) / float(p0) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return pct, str(bars[exit_idx].get("trade_date", "")), actual_n


def _match_bullish_to_closed(
    entries_sorted: list[dict],
    closed_positions: list[dict],
) -> dict[int, tuple[int, float]]:
    """多头 journal ↔ 已结仓的去重匹配（贪心，按 delta 升序）。

    每笔 closed 至多验证 1 条 bullish journal，每条 bullish journal 至多归因 1 笔 closed。
    返回 {journal_idx → (closed_idx, pnl_pct)}。
    """
    pairs: list[tuple[int, int, int, float]] = []  # (delta, j_idx, c_idx, pnl_pct)
    for j_idx, e in enumerate(entries_sorted):
        if e.get("verdict") not in BULLISH_VERDICTS:
            continue
        try:
            j_d = date.fromisoformat(e.get("date", ""))
        except (TypeError, ValueError):
            continue
        for c_idx, c in enumerate(closed_positions):
            try:
                ed = date.fromisoformat((c.get("open") or {}).get("entry_date", ""))
            except (TypeError, ValueError):
                continue
            delta = (ed - j_d).days
            if 0 <= delta <= 14:
                pnl = (c.get("close") or {}).get("realized_pnl_pct")
                if pnl is not None:
                    pairs.append((delta, j_idx, c_idx, float(pnl) * 100))
    pairs.sort(key=lambda t: t[0])
    used_j: set[int] = set()
    used_c: set[int] = set()
    out: dict[int, tuple[int, float]] = {}
    for delta, j_idx, c_idx, pnl in pairs:
        if j_idx in used_j or c_idx in used_c:
            continue
        used_j.add(j_idx)
        used_c.add(c_idx)
        out[j_idx] = (c_idx, pnl)
    return out


def _resolve_outcome_for_entry(entry: dict,
                                closed_positions: list[dict],
                                active_positions: list[dict],
                                current_price: Optional[float]) -> str:
    """B1：把一条 journal entry 关联到实际开仓结果。

    匹配规则：同 ts_code，position 的 entry_date 在 [journal.date, journal.date + 14d]。
    优先 closed（结果已知），否则 active（持仓中），否则 "未跟进"。
    """
    j_date = entry.get("date") or ""
    if not j_date:
        return ""
    try:
        j_d = date.fromisoformat(j_date)
    except (TypeError, ValueError):
        return ""

    def _within_window(e_date: str) -> bool:
        try:
            d = date.fromisoformat(e_date)
        except (TypeError, ValueError):
            return False
        return 0 <= (d - j_d).days <= 14

    # 优先匹配 closed_positions（取 entry_date 距 j_date 最近的）
    matched_closed: list[tuple[int, dict]] = []
    for c in closed_positions:
        e_date = (c.get("open") or {}).get("entry_date") or ""
        if not _within_window(e_date):
            continue
        try:
            delta = (date.fromisoformat(e_date) - j_d).days
        except Exception:
            delta = 999
        matched_closed.append((delta, c))
    if matched_closed:
        matched_closed.sort(key=lambda t: t[0])
        c = matched_closed[0][1]
        cl = c.get("close") or {}
        pnl = cl.get("realized_pnl_pct")
        days = cl.get("days_held", "?")
        reason = cl.get("exit_reason", "")
        if pnl is not None:
            return f" → **实际 {pnl * 100:+.2f}%**（持有 {days} 天，{reason}）"
        return f" → 已平仓（{reason}）"

    # 再看 active_positions
    matched_active: list[tuple[int, dict]] = []
    for p in active_positions:
        e_date = p.get("entry_date") or ""
        if not _within_window(e_date):
            continue
        try:
            delta = (date.fromisoformat(e_date) - j_d).days
        except Exception:
            delta = 999
        matched_active.append((delta, p))
    if matched_active:
        matched_active.sort(key=lambda t: t[0])
        p = matched_active[0][1]
        e_p = p.get("entry_price")
        e_date = p.get("entry_date", "?")
        if current_price is not None and e_p:
            unrealized = (float(current_price) - float(e_p)) / float(e_p) * 100
            return f" → **持仓中 {unrealized:+.2f}%**（开仓 {e_date} @ {e_p}, 现价 {current_price}）"
        return f" → 持仓中（开仓 {e_date}）"

    return " → 未跟进（未开仓）"


# ── 24h 重复分析限幅（痛点#3透明化）─────────────────────────────────────
# 技术面变化阈值：用于客观交叉验证 AI 声称的"本次有新增信息"。
# 任一关键指标变化超阈值即视为客观有变化；全部低于阈值 -> AI 空口声称，不豁免限幅。
_FEATURE_DIFF_THRESHOLDS = {
    "price_vs_ma5_pct": 3.0,   # 百分点
    "rsi_14": 10.0,            # RSI 单位
    "volume_ratio": 0.3,       # 量比
}


def _verdict_index(verdict: str) -> int:
    """verdict 在 VERDICT_ENUM 中的档位 index，未找到返回 -1。"""
    try:
        return VERDICT_ENUM.index(verdict)
    except ValueError:
        return -1


def _clamp_verdict_delta(last_verdict: str, raw_verdict: str, max_delta: int = 1) -> str:
    """把方向变化限制在 ±max_delta 档内，返回 clamp 后的 verdict。
    last 或 raw 不在 ENUM 内时原样返回 raw（无法判定档位差不强制）。"""
    last_idx = _verdict_index(last_verdict)
    raw_idx = _verdict_index(raw_verdict)
    if last_idx < 0 or raw_idx < 0:
        return raw_verdict
    delta = raw_idx - last_idx
    if abs(delta) <= max_delta:
        return raw_verdict
    clamped_idx = last_idx + max(-max_delta, min(max_delta, delta))
    clamped_idx = max(0, min(len(VERDICT_ENUM) - 1, clamped_idx))
    return VERDICT_ENUM[clamped_idx]


def _features_changed(last_feat: dict, cur_feat: dict) -> bool:
    """客观交叉验证：本次 features vs 上次，任一关键指标变化超阈值即 True。
    用于驳回 AI 空口声称"有新增信息"（防编造/漂移，宁可错杀）。"""
    for key, thresh in _FEATURE_DIFF_THRESHOLDS.items():
        lv = last_feat.get(key)
        cv = cur_feat.get(key)
        if lv is None or cv is None:
            continue
        try:
            if abs(float(cv) - float(lv)) >= thresh:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _compute_repeat_analysis(
    history_entries: list[dict],
    raw_verdict: str,
    raw_confidence: Optional[int],
    new_info: list,
    cur_features: dict,
) -> dict:
    """计算本次分析相比上次的 24h 重复限幅信息。

    - is_repeat: 距最近一次分析 < 24h（客观硬条件）
    - new_info_verified: AI 自报有新增 且 技术面客观变化超阈值
    - 限幅由调用方根据 is_repeat & not new_info_verified 执行（宁可错杀）
    """
    out: dict = {
        "is_repeat": False,
        "hours_since_last": None,
        "last_verdict": None,
        "last_confidence": None,
        "last_analyzed_at": None,
        "verdict_delta": 0,
        "confidence_delta": 0,
        "new_info_claimed": list(new_info or []),
        "new_info_verified": False,
        "limited": False,
        "limit_rule": None,
        "raw_verdict": raw_verdict,
        "raw_confidence": raw_confidence,
    }
    if not history_entries:
        return out
    last = history_entries[-1]  # load_entries 按时间正序，最后一条即最近
    last_at = last.get("analyzed_at") or last.get("date")
    if not last_at:
        return out
    try:
        last_dt = datetime.fromisoformat(last_at)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=_TZ_CN)
    except (ValueError, TypeError):
        return out
    now_dt = datetime.now(_TZ_CN)
    hours = (now_dt - last_dt).total_seconds() / 3600.0
    out["hours_since_last"] = round(hours, 2)
    if hours < 0 or hours >= 24:
        return out  # 不在 24h 内，不算重复
    out["is_repeat"] = True
    out["last_verdict"] = last.get("verdict")
    out["last_confidence"] = last.get("confidence")
    out["last_analyzed_at"] = last_at
    last_idx = _verdict_index(last.get("verdict", ""))
    raw_idx = _verdict_index(raw_verdict)
    if last_idx >= 0 and raw_idx >= 0:
        out["verdict_delta"] = raw_idx - last_idx
    if raw_confidence is not None and last.get("confidence") is not None:
        try:
            out["confidence_delta"] = int(raw_confidence) - int(last["confidence"])
        except (TypeError, ValueError):
            pass
    # 客观交叉验证：AI 声称有新增 + 技术面真的变了 -> 才豁免限幅
    if out["new_info_claimed"]:
        out["new_info_verified"] = _features_changed(last.get("features") or {}, cur_features or {})
    return out


def _apply_repeat_limit(
    repeat_info: dict,
    raw_verdict: str,
    raw_confidence: Optional[int],
) -> tuple[str, Optional[int]]:
    """根据 repeat_info 决定是否限幅，返回 (limited_verdict, limited_confidence)。

    24h 内重复 + 无客观新增 -> 方向 ±1 档 / conf ±2（宁可错杀，防 LLM 漂移）。
    就地修改 repeat_info['limited'] / ['limit_rule']。
    """
    limited_verdict = raw_verdict
    limited_confidence = raw_confidence
    if repeat_info["is_repeat"] and not repeat_info["new_info_verified"]:
        rules = []
        if abs(repeat_info["verdict_delta"]) > 1:
            limited_verdict = _clamp_verdict_delta(
                repeat_info["last_verdict"], raw_verdict, max_delta=1,
            )
            if limited_verdict != raw_verdict:
                rules.append("verdict_delta_clamped")
        if raw_confidence is not None and repeat_info["last_confidence"] is not None:
            try:
                delta = int(raw_confidence) - int(repeat_info["last_confidence"])
                if abs(delta) > 2:
                    limited_confidence = int(repeat_info["last_confidence"]) + max(-2, min(2, delta))
                    limited_confidence = max(1, min(10, limited_confidence))
                    rules.append("conf_clamped")
            except (TypeError, ValueError):
                pass
        if rules:
            repeat_info["limited"] = True
            repeat_info["limit_rule"] = "+".join(rules)
    return limited_verdict, limited_confidence


def _finalize_verdict_candidate(
    candidate: dict, history_entries: list[dict],
) -> tuple[dict, dict]:
    """Apply deterministic post-model limits before prose generation and persistence."""
    final = dict(candidate or {})
    raw_verdict = str(final.get("verdict") or "")
    raw_confidence = final.get("confidence")
    if raw_confidence is not None:
        try:
            raw_confidence = max(1, min(10, int(raw_confidence)))
        except (TypeError, ValueError):
            raw_confidence = None
    repeat_info = _compute_repeat_analysis(
        history_entries,
        raw_verdict,
        raw_confidence,
        final.get("new_info", []),
        final.get("features", {}),
    )
    final["verdict"], final["confidence"] = _apply_repeat_limit(
        repeat_info, raw_verdict, raw_confidence,
    )
    return final, {"repeat_analysis": repeat_info}


def _audit_repeat_candidate(
    candidate: dict, history_entries: list[dict],
) -> tuple[dict, dict]:
    """Record 24h repeat drift without mutating a decision-policy-v1 result."""
    final = dict(candidate or {})
    repeat_info = _compute_repeat_analysis(
        history_entries,
        str(final.get("verdict") or ""),
        final.get("confidence"),
        final.get("new_info", []),
        final.get("features", {}),
    )
    suggested_verdict, suggested_confidence = _apply_repeat_limit(
        repeat_info, str(final.get("verdict") or ""), final.get("confidence"),
    )
    repeat_info["legacy_suggested_verdict"] = suggested_verdict
    repeat_info["legacy_suggested_confidence"] = suggested_confidence
    repeat_info["limited"] = False
    repeat_info["limit_rule"] = None
    return final, {"repeat_analysis": repeat_info}


def _format_history(entries: list[dict], ts_code: str, limit: int = 8) -> str:
    """Format past journal entries for injection into the user prompt.

    B1: 每条历史尾部追加"实际后续表现"——从 closed/active positions 反查实际盈亏。
    """
    if not entries:
        return "（无历史记录，本次为首次分析）"

    # 一次性拉该 ts_code 的相关 closed + active positions（B1 数据源）
    closed_for_code: list[dict] = []
    active_for_code: list[dict] = []
    current_price: Optional[float] = None
    try:
        from apex import watchlist as _wl
        wl = _wl.load()
        active_for_code = [p for p in wl.get("active_positions", []) if p.get("ts_code") == ts_code]
        try:
            closed_for_code = [r for r in _wl.load_closed_positions() if r.get("ts_code") == ts_code]
        except Exception:
            closed_for_code = []
    except Exception:
        pass

    if active_for_code:
        try:
            rt = data.get_realtime_price([ts_code]).get(ts_code)
            current_price = rt
            if not current_price:
                current_price = data.get_latest_price([ts_code]).get(ts_code)
        except Exception:
            current_price = None

    all_entries_sorted = sorted(
        entries,
        key=lambda e: e.get("analyzed_at") or e.get("date", ""),
        reverse=True,
    )
    recent = all_entries_sorted[:limit]

    # 拉取覆盖所有 journal 日期的日 K 线，用于空头/未匹配多头的前向收益验证
    bars: list[dict] = []
    valid_dates: list[date] = []
    for e in all_entries_sorted:
        try:
            valid_dates.append(date.fromisoformat(e.get("date") or ""))
        except (TypeError, ValueError):
            continue
    if valid_dates:
        bars = _fetch_forward_bars(
            ts_code,
            min(valid_dates) - timedelta(days=5),
            max(valid_dates) + timedelta(days=30),
        )

    # 多头 ↔ 已结仓：去重 1:1 匹配
    bullish_match = _match_bullish_to_closed(all_entries_sorted, closed_for_code)
    confirmed_bullish: list[float] = [pnl for _, pnl in bullish_match.values()]

    # 空头 → 10 日前向收益（A 股不能做空，用价格本身验证 "避开后确实跌了" 的判断）
    confirmed_bearish: list[float] = []
    outcome_by_idx: dict[int, str] = {}
    for j_idx, e in enumerate(all_entries_sorted):
        v = e.get("verdict", "")
        # 多头匹配到已结仓 → 直接用实盘 pnl
        if j_idx in bullish_match:
            c_idx, pnl = bullish_match[j_idx]
            cl = closed_for_code[c_idx].get("close") or {}
            days = cl.get("days_held", "?")
            reason = cl.get("exit_reason", "")
            outcome_by_idx[j_idx] = f" → **实际 {pnl:+.2f}%**（持有 {days} 天，{reason}）"
            continue
        # 否则先看是否有持仓中匹配
        active_outcome = _resolve_outcome_for_entry(e, [], active_for_code, current_price)
        if active_outcome and "持仓中" in active_outcome:
            outcome_by_idx[j_idx] = active_outcome
            continue
        # 最后用前向收益评估方向类判断（中性/观望不算）
        if v in BULLISH_VERDICTS or v in BEARISH_VERDICTS:
            try:
                j_d = date.fromisoformat(e.get("date") or "")
            except (TypeError, ValueError):
                outcome_by_idx[j_idx] = " → 未跟进（日期缺失）"
                continue
            fr = _forward_return_pct(j_d, bars, n_trading_days=10, min_trading_days=5)
            if fr is None:
                outcome_by_idx[j_idx] = " → 未跟进（前向数据不足）"
                continue
            pct, _, actual_n = fr
            if v in BEARISH_VERDICTS:
                tag = "✅看跌命中" if pct < 0 else "❌看跌打脸"
                confirmed_bearish.append(pct)
                outcome_by_idx[j_idx] = f" → {actual_n}日后 {pct:+.2f}% {tag}"
            else:
                # 多头但未实际开仓 → 仅作为参考显示，不计入统计（避免实盘 pnl 与纸面收益混算）
                tag = "✅看涨命中" if pct > 0 else "❌看涨打脸"
                outcome_by_idx[j_idx] = f" → {actual_n}日后 {pct:+.2f}% {tag}（未跟进，纸面）"
        else:
            outcome_by_idx[j_idx] = " → 未跟进（未开仓）"

    confs_all: list[int] = []
    for e in all_entries_sorted:
        c = e.get("confidence")
        if c is not None:
            try:
                confs_all.append(int(c))
            except (TypeError, ValueError):
                pass

    stat_lines = []
    if confirmed_bullish:
        hit = sum(1 for p in confirmed_bullish if p > 0)
        med = sorted(confirmed_bullish)[len(confirmed_bullish) // 2]
        stat_lines.append(
            f"多头判断 {len(confirmed_bullish)} 笔（实盘 pnl）→ 盈利 {hit} 笔 "
            f"(命中率 {hit/len(confirmed_bullish)*100:.0f}%)，中位收益 {med:+.1f}%"
        )
    if confirmed_bearish:
        hit = sum(1 for p in confirmed_bearish if p < 0)
        med = sorted(confirmed_bearish)[len(confirmed_bearish) // 2]
        stat_lines.append(
            f"空头判断 {len(confirmed_bearish)} 次（5~10日前向收益）→ 后市下跌 {hit} 次 "
            f"(看跌命中率 {hit/len(confirmed_bearish)*100:.0f}%)，中位收益 {med:+.1f}%"
        )
    if confs_all:
        avg_conf = sum(confs_all) / len(confs_all)
        stat_lines.append(f"历史平均置信度 {avg_conf:.1f}/10（共 {len(confs_all)} 次）")

    stat_block = (
        f"### 命中率统计（多头={len(confirmed_bullish)}笔实盘 / "
        f"空头={len(confirmed_bearish)}次前向收益）\n"
        + ("\n".join(f"- {s}" for s in stat_lines) if stat_lines else "- 暂无可验证样本")
        + "\n（命中率低对置信度的影响见下方"
        "「置信度调整」表，不要在这里另算）"
        "\n\n### 近 {} 次分析记录\n".format(len(recent))
    )

    idx_by_id = {id(e): i for i, e in enumerate(all_entries_sorted)}
    lines = []
    for e in recent:
        j_idx = idx_by_id.get(id(e), -1)
        outcome = outcome_by_idx.get(j_idx, " → 未跟进（未开仓）")
        lines.append(history_digest(e, outcome))
    return stat_block + "\n".join(lines)


def _resolve_industries_batch(ts_codes: list[str]) -> dict[str, str]:
    """B7 helper: 批量拿 ts_code → industry。1 次 tushare 调用搞定。失败返回空。"""
    if not ts_codes:
        return {}
    try:
        pro = data._tushare()
        df = pro.stock_basic(
            ts_code=",".join(ts_codes),
            fields="ts_code,industry",
        )
        if df is None or df.empty:
            return {}
        return {
            str(row["ts_code"]): str(row.get("industry", "") or "未知")
            for _, row in df.iterrows()
        }
    except Exception:
        return {}


def _format_held_ladder_block(held: dict, ts_code: str) -> str:
    """v1.1.0 OV#6: 候选已被持有 -> 注入当前 ladder 快照 + 指示调 record_position_action（非 record_verdict）。

    当前 ladder 是冻结基线；模型必须显式选择 preserve、replace 或 clear。
    """
    shares = held.get("position_size_shares")
    avg = held.get("avg_cost") or held.get("entry_price")
    stop = held.get("stop_loss")
    target = held.get("target")
    plan = held.get("plan") or {}
    scale_plan = plan.get("scale_plan") or []
    parts = [
        f"## ⚠ 你已持有 {ts_code}（{held.get('name', '')}）",
        f"当前持仓：{shares}股 @ {avg}，止损 {stop}，目标 {target}。",
        "**本次是重新分析已持仓票，必须调 `record_position_action` 给加减仓建议**"
        "（action=hold/add/trim/exit + 对应股数/比例 + new_stop + ladder_intent），"
        "调 `record_verdict` 会被系统拒绝。",
    ]
    if scale_plan:
        parts.append("当前 ladder（冻结基线，供本次显式选择）：")
        for item in scale_plan:
            tag = "✓已触发" if item.get("executed") else "待触发"
            parts.append(
                f"- L{item.get('level', '?')} {item.get('action', '?')} @ {item.get('trigger_price')} "
                f"-> new_stop {item.get('new_stop')}（{tag}）{item.get('reason', '')}"
            )
        parts.append(
            "ladder_intent 必填：preserve：不提交 scale_plan，保留上述完整 ladder；"
            "replace：提交完整的期望 ladder（会整体替换当前计划，不得只提交新增或调整档）；"
            "clear：明确清空 ladder，不提交 scale_plan。"
        )
    else:
        parts.append(
            "当前 ladder 为空（首次重新分析）。ladder_intent 必填："
            "replace：提交完整的非空 scale_plan（首加/首减触发价 + 止损上移节奏 + 减仓比例，锚定当前 stop/target）；"
            "preserve：保持空 ladder，不提交 scale_plan；"
            "clear：明确清空 ladder，不提交 scale_plan。"
        )
    return "\n".join(parts)


def _format_portfolio_context(
    candidate_ts_code: str, position_baseline: dict | None = None,
) -> str:
    """B7：当前持仓上下文，注入用户 prompt 让 AI 知道行业集中度 / 总风险。"""
    try:
        from apex import watchlist as _wl
        from apex import account as _account
        wl = _wl.load()
        positions = wl.get("active_positions", []) or []
        if not positions:
            return "## 你的当前持仓\n（你目前空仓，无相关性 / 集中度问题，仓位上限 = 单笔 + 总风险约束）"

        # 批量拉行业（持仓 + 候选）
        all_codes = list({p.get("ts_code") for p in positions if p.get("ts_code")})
        all_codes.append(candidate_ts_code)
        industries = _resolve_industries_batch(all_codes)
        candidate_industry = industries.get(candidate_ts_code, "未知")

        # 行业分布
        ind_count: dict[str, int] = {}
        for p in positions:
            ts = p.get("ts_code")
            ind = industries.get(ts, "未知") or "未知"
            ind_count[ind] = ind_count.get(ind, 0) + 1
        ind_dist = ", ".join(
            f"{ind}×{cnt}" for ind, cnt in
            sorted(ind_count.items(), key=lambda kv: -kv[1])
        )

        # 总风险 + 资金占用
        account = _account.load()
        risk_summary = _account.current_total_risk(positions, account=account)
        capital = float(account.get("total_capital") or 0)
        total_capital_used = sum(
            float((p.get("position_size_shares") or 0))
            * float(p.get("entry_price") or 0)
            for p in positions
        )

        # 同行业持仓
        same_ind_codes = [
            p.get("ts_code") for p in positions
            if industries.get(p.get("ts_code"), "") == candidate_industry
            and candidate_industry and candidate_industry != "未知"
        ]

        held = position_baseline if position_baseline and position_baseline.get("ts_code") == candidate_ts_code else next(
            (p for p in positions if p.get("ts_code") == candidate_ts_code), None
        )
        lines = [
            f"## 你的当前持仓上下文（{len(positions)} 只）",
            f"- 行业分布：{ind_dist}",
        ]
        if held:
            lines.insert(0, _format_held_ladder_block(held, candidate_ts_code))
        if capital > 0:
            lines.append(
                f"- 资金占用：{total_capital_used:,.0f} / {capital:,.0f} "
                f"（{total_capital_used / capital * 100:.1f}%）"
            )
        if risk_summary.get("total_risk_pct") is not None:
            limit_pct = risk_summary["max_total_risk_pct"]
            lines.append(
                f"- 总风险敞口：{risk_summary['total_risk_amount']:,.0f} 元 "
                f"({risk_summary['total_risk_pct']:.2f}% 占账户) — 上限 {limit_pct}%"
                + (" **⚠ 已超上限**" if risk_summary.get("over_limit") else "")
            )
        if candidate_industry and candidate_industry != "未知":
            if same_ind_codes:
                lines.append(
                    f"- ⚠ **本次分析的 {candidate_ts_code} 行业是 {candidate_industry}，"
                    f"你已持有同行业 {len(same_ind_codes)} 只**：{', '.join(same_ind_codes)}。"
                    f"若继续加仓需明确说明：是否会推高行业集中度风险？同行业持仓相关性高，"
                    f"系统性风险（行业 -10%）可能让多个持仓同时打止损。"
                )
            else:
                lines.append(
                    f"- 本次分析 {candidate_ts_code} 行业 {candidate_industry}，"
                    f"与现有持仓无重叠（分散度 OK）。"
                )

        lines.append(
            "\n**判断时必须考虑**："
            "(a) 是否推高行业集中度？(b) 总风险是否还有余量？(c) 与现有持仓是对冲还是重叠？"
            "若加仓后会突破单一行业 ≥3 只或总风险逼近上限，结论里必须明确建议"
            "**减仓 / 等待 / 替换持仓**而不是无脑加（对置信度的影响见下方调整表）。"
        )
        return "\n".join(lines)
    except Exception as e:
        return f"## 你的当前持仓上下文\n（加载失败: {type(e).__name__}: {e}）"


def _format_intraday_block(ts_code: str) -> tuple[str, dict]:
    """个股盘中分时走势快照，注入 prompt。返回 (formatted_str, raw_dict)。

    盘中时为实时走势（当日尚未收盘），收盘后为当日完整走势。
    fail-soft：加载失败时提示 AI 以 get_daily_price 最新 bar 为准。
    """
    try:
        raw = data.get_intraday_snapshot(ts_code)
        ctx = json.loads(raw)
    except Exception as e:
        return f"## 盘中实时走势\n（加载失败: {type(e).__name__}: {e}）", {}

    if not ctx or not ctx.get("last_price"):
        return ("## 盘中实时走势\n（数据加载失败，请以 get_daily_price 最新 bar 为准）", ctx)

    intra_label = "盘中实时" if ctx.get("is_intraday") else "当日"
    lines = [f"## {intra_label}走势（截至 {ctx.get('as_of_time', '?')}）"]

    parts = [f"现价 {ctx['last_price']}"]
    if ctx.get("prev_close") is not None:
        parts.append(f"昨收 {ctx['prev_close']}")
    if ctx.get("day_chg_pct") is not None:
        parts.append(f"日内 {ctx['day_chg_pct']:+.2f}%")
    if ctx.get("open") is not None:
        parts.append(f"开盘 {ctx['open']}")
    if ctx.get("high") is not None and ctx.get("low") is not None:
        parts.append(f"高/低 {ctx['high']}/{ctx['low']}")
    if ctx.get("amplitude_pct") is not None:
        parts.append(f"振幅 {ctx['amplitude_pct']:.2f}%")
    lines.append(f"- {' / '.join(parts)}")

    parts2 = []
    if ctx.get("vwap") is not None:
        parts2.append(f"VWAP {ctx['vwap']}")
    if ctx.get("vwap_position_pct") is not None:
        pos = ctx["vwap_position_pct"]
        pos_label = "盘上偏强" if pos > 0 else "盘下偏弱"
        parts2.append(f"现价{pos_label}({pos:+.2f}%)")
    if ctx.get("shape"):
        parts2.append(f"形态 {ctx['shape']}")
    if ctx.get("vol_ratio") is not None:
        parts2.append(f"量比{ctx['vol_ratio']:.2f}({ctx.get('vol_label') or ''})")
    if ctx.get("amount_yi") is not None:
        parts2.append(f"成交额 {ctx['amount_yi']:.2f}亿")
    if parts2:
        lines.append(f"- {' / '.join(parts2)}")

    if ctx.get("is_intraday"):
        lines.append("- ⚠ 盘中数据，尚未收盘；需与 get_daily_price 最新 bar（上一交易日）结合判断当日强弱。")
    else:
        lines.append("- 当日已收盘，可与日线最新 bar 对齐。")

    # 量价段分析（早/中/尾盘）
    segs = ctx.get("session_segments")
    if segs:
        seg_parts = []
        for s in segs:
            if s.get("chg_pct") is None:
                continue
            vol_pct = s.get("vol_pct")
            vol_str = f"量占{vol_pct:.0f}%" if vol_pct is not None else ""
            seg_parts.append(f"{s['name']}{s['chg_pct']:+.2f}%{vol_str}")
        if seg_parts:
            lines.append(f"- 分段：{' / '.join(seg_parts)}")

    # 盘中拐点/回吐
    sw = ctx.get("swing")
    if sw and sw.get("verdict"):
        lines.append(f"- 拐点：{sw['verdict']}（高{sw.get('high_time')} {sw.get('high_pct')}% / 低{sw.get('low_time')} {sw.get('low_pct')}%）")

    # 量价配合
    vp = ctx.get("vol_price_match")
    if vp and vp.get("verdict"):
        lines.append(f"- 量价：{vp['verdict']}")

    return "\n".join(lines), ctx


def _format_market_context(ts_code: str) -> tuple[str, dict]:
    """大盘 / 板块 / 资金面 / 个股相对强度 context。

    返回 (formatted_str, raw_dict)：前者塞 prompt，后者存 journal。
    设计原则：把判断逻辑（强势 / 弱势 / 跑赢）先在 Python 里算成 regime 标签，
    AI 看到的是已经翻译过的结论而不是一堆数字，减小误读概率。
    """
    try:
        raw = data.get_market_context(ts_code)
        ctx = json.loads(raw)
    except Exception as e:
        return f"## 市场 context\n（加载失败: {type(e).__name__}: {e}）", {}

    if not ctx.get("indices") and not ctx.get("sector") and not ctx.get("north_money"):
        return "## 市场 context\n（数据全部加载失败，本次分析不可用此项）", ctx

    lines = [f"## 市场 / 板块 / 资金面 context（截至 {ctx.get('as_of', '?')}）"]

    # 大盘
    if ctx.get("indices"):
        lines.append("\n### 大盘指数")
        for idx in ctx["indices"]:
            parts = [f"{idx['name']} {idx['close']}"]
            if idx.get("daily_chg_pct") is not None:
                trade_date_label = idx.get("trade_date", "上一个交易日")
                parts.append(f"{trade_date_label} {idx['daily_chg_pct']:+.2f}%")
            if idx.get("chg_5d_pct") is not None:
                parts.append(f"5日 {idx['chg_5d_pct']:+.2f}%")
            if idx.get("chg_20d_pct") is not None:
                parts.append(f"20日 {idx['chg_20d_pct']:+.2f}%")
            if idx.get("position_60d_pct") is not None:
                parts.append(f"60日位置 {idx['position_60d_pct']:.0f}%")
            if idx.get("vol_ratio_5d") is not None:
                v = idx["vol_ratio_5d"]
                vol_label = "放量" if v > 1.2 else ("缩量" if v < 0.8 else "平量")
                parts.append(f"量比 {v:.2f}({vol_label})")
            lines.append(f"- {' / '.join(parts)}")

    # 大盘当日分时（沪深300 压缩特征）—— 日线只有收盘涨跌，这里补上盘中形态/分段/拐点/量价
    idx_intra = ctx.get("index_intraday")
    if idx_intra and idx_intra.get("last_price"):
        intra_label = "盘中实时" if idx_intra.get("is_intraday") else "当日"
        lines.append(f"\n### 大盘当日分时（沪深300 {intra_label}，截至 {idx_intra.get('as_of_time', '?')}）")
        iparts = [f"现价 {idx_intra['last_price']}"]
        if idx_intra.get("prev_close") is not None:
            iparts.append(f"昨收 {idx_intra['prev_close']}")
        if idx_intra.get("day_chg_pct") is not None:
            iparts.append(f"日内 {idx_intra['day_chg_pct']:+.2f}%")
        if idx_intra.get("open") is not None:
            iparts.append(f"开盘 {idx_intra['open']}")
        if idx_intra.get("high") is not None and idx_intra.get("low") is not None:
            iparts.append(f"高/低 {idx_intra['high']}/{idx_intra['low']}")
        if idx_intra.get("amplitude_pct") is not None:
            iparts.append(f"振幅 {idx_intra['amplitude_pct']:.2f}%")
        lines.append(f"- {' / '.join(iparts)}")
        if idx_intra.get("shape"):
            lines.append(f"- 形态 {idx_intra['shape']}")
        segs = idx_intra.get("session_segments")
        if segs:
            seg_parts = []
            for s in segs:
                if s.get("chg_pct") is None:
                    continue
                vol_pct = s.get("vol_pct")
                vol_str = f"量占{vol_pct:.0f}%" if vol_pct is not None else ""
                seg_parts.append(f"{s['name']}{s['chg_pct']:+.2f}%{vol_str}")
            if seg_parts:
                lines.append(f"- 分段：{' / '.join(seg_parts)}")
        sw = idx_intra.get("swing")
        if sw and sw.get("verdict"):
            lines.append(f"- 拐点：{sw['verdict']}")
        vp = idx_intra.get("vol_price_match")
        if vp and vp.get("verdict"):
            lines.append(f"- 量价：{vp['verdict']}")

    # 板块
    sector = ctx.get("sector")
    if sector:
        parts = []
        if sector.get("daily_chg_pct") is not None:
                trade_date_label = sector.get("trade_date", "上一个交易日")
                parts.append(f"{trade_date_label} {sector['daily_chg_pct']:+.2f}%")
        if sector.get("chg_5d_pct") is not None:
            parts.append(f"5日 {sector['chg_5d_pct']:+.2f}%")
        if sector.get("chg_20d_pct") is not None:
            parts.append(f"20日 {sector['chg_20d_pct']:+.2f}%")
        lines.append(f"\n### 个股所属板块（{sector.get('name', '?')}）")
        lines.append(f"- {' / '.join(parts) if parts else '（数据缺失）'}")
    else:
        lines.append("\n### 个股所属板块\n- （未能匹配申万 L1 行业，跳过；可能 tushare 权限不足）")

    # 个股相对
    sr = ctx.get("stock_relative")
    if sr:
        lines.append(f"\n### 个股 {ts_code} 相对强度")
        self_parts = []
        if sr.get("chg_5d_pct") is not None:
            self_parts.append(f"5日 {sr['chg_5d_pct']:+.2f}%")
        if sr.get("chg_20d_pct") is not None:
            self_parts.append(f"20日 {sr['chg_20d_pct']:+.2f}%")
        if self_parts:
            lines.append(f"- 自身涨幅：{' / '.join(self_parts)}")
        if sr.get("vs_index_5d_pct") is not None:
            diff = sr["vs_index_5d_pct"]
            label = "跑赢" if diff > 0 else "跑输"
            lines.append(f"- vs {sr.get('ref_index_name', '大盘')} 5日：{label} {abs(diff):.2f}%")
        if sr.get("vs_sector_5d_pct") is not None:
            diff = sr["vs_sector_5d_pct"]
            label = "跑赢" if diff > 0 else "跑输"
            lines.append(f"- vs {sr.get('sector_name', '板块')} 5日：{label} {abs(diff):.2f}%")

    # 北向
    nm = ctx.get("north_money")
    if nm:
        parts = []
        if nm.get("today_yi") is not None:
            t = nm["today_yi"]
            trade_dates = nm.get("trade_dates", [])
            label_date = trade_dates[-1] if trade_dates else "上一个交易日"
            parts.append(f"{label_date}{'净流入' if t >= 0 else '净流出'} {abs(t):.2f} 亿")
        if nm.get("cumulative_5d_yi") is not None:
            c = nm["cumulative_5d_yi"]
            parts.append(f"5日累计{'净流入' if c >= 0 else '净流出'} {abs(c):.2f} 亿")
        if parts:
            lines.append("\n### 资金面（北向）")
            lines.append(f"- {' / '.join(parts)}")

    # 综合 regime 标签 —— 把判断写死在 Python 里，AI 直接读结论
    # 市场情绪面（涨停/跌停/炸板/连板 -> 三维度 score + regime + market_style）
    ms = ctx.get("market_sentiment")
    if ms:
        lines.append(f"\n### 市场情绪面（截至 {ms.get('as_of', '?')}）")
        lines.append(
            f"- 涨停 {ms.get('limit_up_count', '?')} 家 / 跌停 {ms.get('limit_down_count', '?')} 家"
            f" / 炸板 {ms.get('broken_limit_count', '?')} 家（炸板率 {ms.get('broken_rate_pct', '?')}%）"
        )
        up_down = ms.get("up_down_ratio")
        lines.append(
            f"- 最高连板 {ms.get('max_consecutive', '?')} 板 / 强势股池 {ms.get('strong_pool_count', '?')} 家"
            + (f" / 涨停跌停比 {up_down}" if up_down is not None else "")
        )
        sip = ms.get("stock_in_pool") or {}
        if sip.get("limit_up") or sip.get("strong_pool"):
            tags = []
            if sip.get("limit_up"):
                tags.append(f"今日涨停({sip.get('consecutive')}板)" if sip.get("consecutive") else "今日涨停")
            if sip.get("strong_pool"):
                tags.append("在强势股池")
            lines.append(f"- 标的自身：{'+'.join(tags)}（个股情绪强信号）")
        else:
            lines.append("- 标的自身：未在涨停池/强势股池")
        b = ms.get("breadth") or {}
        m = ms.get("momentum") or {}
        r = ms.get("risk_appetite") or {}
        lines.append(
            f"- 三维度：广度 {b.get('level', '?')}({b.get('score', '?')})"
            f" / 接力 {m.get('level', '?')}({m.get('score', '?')})"
            f" / 风险偏好 {r.get('level', '?')}({r.get('score', '?')})"
            f" -> 总分 {ms.get('total_score', '?')}"
        )
        lines.append(
            f"- **情绪 regime：{ms.get('regime', '?')}** / **市场风格：{ms.get('market_style', '?')}**"
        )
        if ms.get("reasons"):
            lines.append(f"- 依据：{'；'.join(ms['reasons'])}")

    lines.append("\n### 综合 regime 判断（Python 预计算，直接引用）")
    regime: list[str] = []

    hs300 = next((i for i in ctx.get("indices", []) if i["code"] == "000300.SH"), None)
    hs300_5d = hs300.get("chg_5d_pct") if hs300 else None
    if hs300_5d is not None:
        if hs300_5d < -2:
            regime.append("**大盘弱势**(沪深300 5日 < -2%)")
        elif hs300_5d > 2:
            regime.append("**大盘强势**(沪深300 5日 > +2%)")
        else:
            regime.append("大盘震荡")

    sector_5d = sector.get("chg_5d_pct") if sector else None
    if sector_5d is not None and hs300_5d is not None:
        rel = sector_5d - hs300_5d
        if rel > 1:
            regime.append(f"**板块强势**({sector['name']}跑赢大盘 {rel:+.1f}%)")
        elif rel < -1:
            regime.append(f"**板块弱势**({sector['name']}跑输大盘 {rel:+.1f}%)")
        else:
            regime.append(f"板块同步({sector['name']})")

    if sr and sr.get("vs_sector_5d_pct") is not None:
        rel = sr["vs_sector_5d_pct"]
        if rel > 1.5:
            regime.append(f"**个股强于板块**({rel:+.1f}%)")
        elif rel < -1.5:
            regime.append(f"**个股弱于板块**({rel:+.1f}% — 板块涨它不涨是危险信号)")

    if nm and nm.get("cumulative_5d_yi") is not None:
        c = nm["cumulative_5d_yi"]
        if c < -100:
            regime.append(f"**北向 5 日大幅净流出** ({c:.0f} 亿)")
        elif c > 100:
            regime.append(f"**北向 5 日大幅净流入** (+{c:.0f} 亿)")

    if regime:
        lines.append("- " + " / ".join(regime))
    else:
        lines.append("- （数据不足，无法形成结论）")

    lines.append(
        "\n**裁判结论必须明确引用以上 regime 标签**；"
        "对置信度的调整见下方「置信度调整」表，不要在这里另算。"
    )

    return "\n".join(lines), ctx


def _format_playstyle_block(ts_code: str) -> tuple[str, dict]:
    """标的玩法特征（Playstyle FE）快照，注入 prompt。返回 (formatted_str, raw_features)。

    仿 _format_market_context / _format_intraday_block：Python 算特征 -> 翻译成结论性标签
    塞 prompt，raw dict 存 journal（T5 落 entry.playstyle_features）。原始行情/信号 bars 不
    进 prompt、不落 trace（与分时一致）。fail-soft：extract_features 绝不抛异常。

    D9 已验收（grounded-LLM 可行）→ 走 AI 填 skill 分支：本函数在 run() 注入 FE 特征 prompt，
    AI 经 record_verdict 填 playstyle ratings，finalize_playstyle 规整 + 软门控（gate#1 auto-fix
    primary / gate#2 flag，OV#6 不 reject）。AI 未填但 FE>=0.5 → Python compute_rule_ratings
    兜底（method=python_fallback）；FE<0.5 → 降级 null（不卡流程）。
    """
    feats = playstyle.extract_features(ts_code)
    f = feats["features"]
    completeness = feats["completeness"]
    risk_level = feats["risk_level"]

    lines = [f"## 玩法特征（Playstyle FE，截至 {feats.get('as_of', '?')}）"]

    if completeness < 0.5:
        lines.append(
            f"- ⚠ 特征完整度 {completeness:.0%} < 50%（数据源失败/停牌），玩法判定降级为 null，"
            "本次不输出 playstyle 星级（系统允许，不卡流程）。"
        )
        if feats.get("notes"):
            lines.append(f"- 降级说明：{'；'.join(feats['notes'])}")
        return "\n".join(lines), feats

    lines.append(f"- 特征完整度 {completeness:.0%} / 风险等级 **{risk_level}**")

    vol = f.get("volatility") or {}
    if vol.get("present"):
        lines.append(
            f"- 波动率：20日 {vol.get('vol_20d_pct', '?')}% / 60日 {vol.get('vol_60d_pct', '?')}%（年化）"
        )

    tr = f.get("turnover") or {}
    if tr.get("present"):
        lines.append(f"- 换手率：20日均值 {tr.get('avg_20d_pct', '?')}%（D11 自推，latest {tr.get('latest_pct', '?')}%）")

    mf = f.get("moneyflow") or {}
    if mf.get("present"):
        sign = {"1": "净流入", "-1": "净流出", "0": "持平"}.get(str(mf.get("sign", 0)), "?")
        lines.append(
            f"- 主力净流入：近5日{sign} {abs(mf.get('sum_yuan', 0) or 0):.0f}元 / 连续正 {mf.get('consec_positive', 0)} 日"
            + ("（部分日缺失）" if mf.get("partial") else "")
        )

    nb = f.get("northbound") or {}
    if nb.get("present"):
        if nb.get("listed_today"):
            lines.append(f"- 北向：今日上榜，净买入 {nb.get('today_inflow_yuan', 0):.0f}元（v1 当日代理）")
        else:
            lines.append("- 北向：今日未上榜（v1 当日代理，5日历史未支持）")

    lu = f.get("limit_up") or {}
    if lu.get("present"):
        lines.append(
            f"- 连板：近10日涨停 {lu.get('count_10d', 0)} 次 / 最高 {lu.get('max_consecutive_10d', 0)} 连板"
        )

    ory = f.get("or_yoy") or {}
    if ory.get("present"):
        sus = "（持续高≥15%）" if ory.get("sustained_high") else ""
        lines.append(
            f"- 业绩：or_yoy 最新 {ory.get('latest', '?')}% / 中位 {ory.get('median', '?')}%"
            f" / 趋势 {ory.get('trend', '?')}{sus}（D12 多季）"
        )

    mv = f.get("circ_mv") or {}
    if mv.get("present"):
        lines.append(f"- 流通市值：{mv.get('yi', '?')} 亿")

    val = f.get("valuation") or {}
    if val.get("present"):
        lines.append(f"- 估值：PE_TTM {val.get('pe_ttm', '?')} / PB {val.get('pb', '?')}")

    roe = f.get("roe") or {}
    if roe.get("present"):
        lines.append(f"- ROE：最新 {roe.get('latest', '?')}%（{'近4季全正' if roe.get('all_positive_4q') else '有季为负或不足4季'}）")

    ma = f.get("ma_alignment") or {}
    if ma.get("present"):
        pb_label = "完整多头排列" if ma.get("perfect_bullish") else "非完整多头"
        lines.append(f"- MA 排列：{ma.get('score', 0)}/3（{pb_label}）")

    lines.append(
        "\n**玩法判定（打野/波段/中线/长线）由星级 prototype 给出，每条原因必须引用以上特征数字"
        "或 web_search 结果，禁凭空编。**"
    )
    return "\n".join(lines), feats


def _stock_profile_from_context(
    ts_code: str, playstyle_feats: dict, candidate_context: dict,
) -> StockProfile:
    """Build the deterministic profile from already-fetched FE plus company metadata."""
    features = (playstyle_feats.get("features") or {}) if isinstance(playstyle_feats, dict) else {}
    industry = str(candidate_context.get("industry") or "")
    if not industry:
        try:
            info = json.loads(data.get_stock_info(ts_code))
            if isinstance(info, list) and info:
                industry = str(info[0].get("industry") or "")
        except Exception:
            pass
    circ_mv = features.get("circ_mv") or {}
    valuation = features.get("valuation") or {}
    turnover = features.get("turnover") or {}
    revenue = features.get("or_yoy") or {}
    profile_features = {
        "circ_mv_yi": circ_mv.get("yi"),
        "pe_ttm": valuation.get("pe_ttm"),
        "turnover_rate": turnover.get("avg_20d_pct") or turnover.get("latest_pct"),
        "revenue_yoy": revenue.get("latest"),
        "industry": industry,
        "dragon_tiger_count_20d": candidate_context.get("dragon_tiger_count_20d", 0),
    }
    return classify_stock_profile(profile_features)


def _system_evidence_items(
    *, ts_code: str, profile: StockProfile, playstyle_feats: dict,
    market_ctx: dict, intraday_ctx: dict,
) -> list[dict]:
    """Expose deterministic injected context through stable ledger IDs."""
    as_of = str(playstyle_feats.get("as_of") or date.today().isoformat()).replace("-", "")
    as_of_iso = f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:8]}" if len(as_of) >= 8 else date.today().isoformat()
    raw = playstyle_feats.get("features") or {}
    definitions = [
        ("technical", {
            key: raw.get(key) for key in ("volatility", "turnover", "ma_alignment") if raw.get(key)
        }),
        ("fundamental", {
            key: raw.get(key) for key in ("or_yoy", "valuation", "roe") if raw.get(key)
        }),
        ("capital", {
            key: raw.get(key) for key in ("moneyflow", "northbound") if raw.get(key)
        }),
    ]
    items: list[dict] = []
    for dimension, facts in definitions:
        if not facts:
            continue
        items.append({
            "id": f"sys_{dimension}_{as_of}",
            "fact": json.dumps(facts, ensure_ascii=False, sort_keys=True),
            "inference": "系统预计算上下文，等待模型判断方向",
            "evidence_type": "structured_data", "tool_name": "system_context",
            "source_name": "Apex Python precompute", "source_url": None,
            "published_at": as_of_iso, "source_tier": 1, "entity_matched": True,
            "freshness_status": "current", "as_of": as_of_iso,
            "frequency": "daily" if dimension != "fundamental" else "quarterly",
            "nature": "current",
            "is_complete": True, "independence_group": f"system_{dimension}:{as_of_iso}",
            "dimension": dimension,
        })
    sentiment = market_ctx.get("market_sentiment") or {}
    if sentiment:
        sentiment_day = str(sentiment.get("as_of") or date.today().isoformat()).replace("-", "")
        sentiment_iso = (
            f"{sentiment_day[:4]}-{sentiment_day[4:6]}-{sentiment_day[6:8]}"
            if len(sentiment_day) >= 8 else date.today().isoformat()
        )
        items.append({
            "id": f"sys_sentiment_{sentiment_day}",
            "fact": json.dumps(sentiment, ensure_ascii=False, sort_keys=True, default=str)[:1600],
            "inference": "市场情绪聚合证据；regime/style/三维度不得拆分重复计分",
            "evidence_type": "market_sentiment", "tool_name": "system_context",
            "source_name": "Apex market sentiment", "source_url": None,
            "published_at": sentiment_iso, "source_tier": 1, "entity_matched": True,
            "freshness_status": "current", "as_of": sentiment_iso,
            "frequency": "daily", "is_complete": True,
            "nature": "current",
            "independence_group": f"market_sentiment:{sentiment_iso}", "dimension": "sentiment",
        })
    if intraday_ctx:
        items.append({
            "id": f"sys_intraday_{date.today().isoformat()}",
            "fact": json.dumps(intraday_ctx, ensure_ascii=False, sort_keys=True, default=str)[:1200],
            "inference": "盘中未完成数据，只能作为风险提示，不能确认日线突破或反转",
            "evidence_type": "structured_data", "tool_name": "system_context",
            "source_name": "Apex intraday", "source_url": None,
            "published_at": date.today().isoformat(), "source_tier": 1, "entity_matched": True,
            "freshness_status": "current", "as_of": date.today().isoformat(),
            "frequency": "intraday", "is_complete": False,
            "nature": "current",
            "independence_group": f"intraday:{ts_code}:{date.today().isoformat()}", "dimension": "technical",
        })
    items.append({
        "id": "sys_stock_profile",
        "fact": json.dumps(profile.to_dict(), ensure_ascii=False, sort_keys=True),
        "inference": "确定性软分类与混合权重，不作为方向证据",
        "evidence_type": "classification", "tool_name": "stock_profile",
        "source_name": "Apex stock profile", "source_url": None,
        "published_at": as_of_iso, "source_tier": 1, "entity_matched": True,
        "freshness_status": "current", "as_of": as_of_iso, "frequency": "daily",
        "is_complete": True, "independence_group": "stock_profile", "dimension": "fundamental",
        "nature": "fact",
    })
    return items


def _format_policy_context(profile: StockProfile, evidence_items: list[dict]) -> str:
    ledger = [
        {
            "evidence_id": item["id"], "dimension": item.get("dimension"),
            "as_of": item.get("as_of"), "frequency": item.get("frequency"),
            "nature": item.get("nature"),
            "is_complete": item.get("is_complete"), "independence_group": item.get("independence_group"),
            "fact": item.get("fact"),
        }
        for item in evidence_items if item.get("id") != "sys_stock_profile"
    ]
    return (
        "## 后端确定性决策上下文\n"
        f"- 股票软分类：`{json.dumps(profile.to_dict(), ensure_ascii=False)}`\n"
        "- 最终方向由 Python 根据 evidence_claims 计算；模型 verdict 仅供审计。\n"
        "- 每边可提交 0-3 条 claim；市场情绪派生指标只能引用同一 independence_group 一次。\n"
        f"- 可引用的系统证据账本：`{json.dumps(ledger, ensure_ascii=False)}`"
    )


def _safety_scan_outcome(parsed: dict) -> bool:
    """权威扫描是否执行成功（与是否命中 Tier 1 无关）。"""
    return not bool(parsed.get("error"))


_MATERIAL_REVIEW_MARKERS = (
    "重大冲突", "关键未知", "事实错误",
    "方向性主张无支撑", "影响结论方向且无支撑", "改变结论方向且无支撑",
)
_REVIEW_DIRECTION_MARKERS = (
    "候选方向", "结论方向", "影响结论方向", "改变结论方向", "方向性主张",
)
_REVIEW_UNSUPPORTED_MARKERS = ("无支撑", "缺少支撑", "缺乏支撑", "缺少证据", "缺乏证据", "未经证实")


def _is_explicit_material_review_issue(message: str) -> bool:
    return (
        any(marker in message for marker in _MATERIAL_REVIEW_MARKERS)
        or (
            any(marker in message for marker in _REVIEW_DIRECTION_MARKERS)
            and any(marker in message for marker in _REVIEW_UNSUPPORTED_MARKERS)
        )
    )


def _normalize_review_issues(issues: list) -> list[dict]:
    normalized = []
    for issue in issues or []:
        if isinstance(issue, dict):
            message = str(issue.get("message") or "").strip()
            severity = str(issue.get("severity") or "minor").lower()
        else:
            message = str(issue).strip()
        if message:
            explicit_material = _is_explicit_material_review_issue(message)
            declared_material = (
                bool(issue.get("blocking")) or severity == "material"
                if isinstance(issue, dict) else False
            )
            blocking = explicit_material or declared_material
            normalized.append({
                "message": message,
                "severity": "material" if blocking else "minor",
                "blocking": blocking,
            })
    return normalized


def _review_transition(outcome, issues, revision_count, *, technical_failure=False):
    normalized = _normalize_review_issues(issues)
    messages = [item["message"] for item in normalized]
    if technical_failure:
        return "abstain", messages
    if outcome == "rework":
        return "rework", messages
    material = any(item["blocking"] or item["severity"] == "material" for item in normalized)
    if material:
        return ("revise" if revision_count < 1 else "abstain"), messages
    if messages and revision_count < 1:
        return "revise", messages
    return "pass", messages


def _valuation_basis_conflict(candidate: dict, thesis: str) -> bool:
    if candidate.get("verdict") not in BEARISH_VERDICTS:
        return False
    if candidate.get("valuation_basis") != "non_valuation":
        return False
    thesis_text = str(thesis or "").lower()
    return any(marker in thesis_text for marker in ("估值", "pe", "peg", "市盈率"))


_VERDICT_REPORT_SECTIONS = (
    "## 核心判断",
    "## 基本面分析",
    "## 市场与个股环境",
    "## 四维分析",
    "## 一、多头论点",
    "## 二、空头论点",
    "## 三、裁判结论",
    "### 加权四维评分",
    "### 证据取舍与冲突",
    "### 历史判断复盘",
    "### 玩法与适用周期",
    "### 置信度调整",
    "## 操作建议",
    "## 风险与未知项",
)
_LEGACY_VERDICT_REPORT_SECTIONS = (
    "## 核心判断",
    "## 基本面分析",
    "## 一、多头论点",
    "## 二、空头论点",
    "## 三、裁判结论",
    "### 加权四维评分",
    "### 置信度调整",
    "## 操作建议",
    "## 风险提示",
)
_POSITION_REPORT_SECTIONS = (
    "## 核心判断",
    "## 基本面分析",
    "## 一、多头论点",
    "## 二、空头论点",
    "## 三、裁判结论",
    "### 加权四维评分",
    "## 当前持仓动作",
    "## 条件触发计划",
    "## 风险提示",
)
_REPORT_PROCESS_MARKERS = (
    "让我查询", "让我补充", "现在提交", "等等，重新核算", "我先获取",
)
_VERDICT_REPORT_CANDIDATE_SCALARS = (
    "verdict", "confidence", "calibrated_confidence", "calibration_sample_size",
    "calibration_applied", "calibration_explanation", "evidence_coverage",
    "net_hardness", "proposed_trade_action", "entry_style", "valid_for_days",
    "entry", "entry_low", "entry_high", "stop_loss", "target",
    "position_size_pct", "setup_tag", "stock_type", "valuation_basis",
    "growth_valuation_mode",
)
_VERDICT_REPORT_FEATURE_FIELDS = (
    "ma5_position", "ma20_position", "ma_alignment", "volume_ratio", "macd_zone",
    "rsi_14", "price_vs_ma5_pct", "atr_14_pct", "candle_direction",
    "candle_body_pct", "candle_upper_shadow_pct", "candle_lower_shadow_pct",
    "candle_pattern",
)
_DECISION_POLICY_REPORT_SCALARS = (
    "verdict", "direction_allowed", "net_hardness", "evidence_coverage",
    "policy_version",
)
_DECISION_DIMENSIONS = ("technical", "fundamental", "capital", "sentiment")


def _report_labeled_field_pattern(label: str) -> str:
    return rf"\*\*{re.escape(label)}：([^*\n]+)\*\*"


def _report_labeled_values(text: str, label: str) -> list[str]:
    return [value.strip() for value in re.findall(_report_labeled_field_pattern(label), text)]


def _report_number(value) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", "" if value is None else str(value))
    return float(match.group()) if match else None


def _validate_labeled_number(
    text: str, label: str, expected, issues: list[str], *, conflict_label: str | None = None,
) -> None:
    values = _report_labeled_values(text, label)
    expected_number = _report_number(expected)
    parsed = [
        [float(number) for number in re.findall(r"-?\d+(?:\.\d+)?", value)]
        for value in values
    ]
    if expected_number is None:
        if values:
            issues.append(f"{label}与结构化结果不一致")
        return
    if parsed != [[expected_number]]:
        issues.append(f"{label}与结构化结果不一致")
    if len(values) != 1 or parsed != [[expected_number]]:
        issues.append(f"存在冲突的{conflict_label or label}")


@dataclass(frozen=True)
class _ReportMarkdownHeading:
    canonical: str
    level: int
    start: int
    line_end: int
    content_start: int
    content_end: int


@dataclass(frozen=True)
class _ParsedReportMarkdown:
    source: str
    visible_text: str
    headings: tuple[_ReportMarkdownHeading, ...]

    def sections(self, canonical: str) -> tuple[_ReportMarkdownHeading, ...]:
        return tuple(
            heading for heading in self.headings if heading.canonical == canonical
        )

    def section_text(self, canonical: str) -> str:
        sections = self.sections(canonical)
        if len(sections) != 1:
            return ""
        section = sections[0]
        return self.visible_text[section.content_start:section.content_end]


def _masked_markdown_line(line: str) -> str:
    return re.sub(r"[^\r\n]", " ", line)


def _parse_report_markdown(text: str) -> _ParsedReportMarkdown:
    """Parse real ATX headings once while masking fenced code at stable offsets."""
    heading_rows: list[tuple[str, int, int, int, int]] = []
    visible_lines: list[str] = []
    offset = 0
    fence_char = ""
    fence_length = 0
    for line in text.splitlines(keepends=True):
        if fence_char:
            closing = re.match(r"^ {0,3}(`{3,}|~{3,})[ \t]*(?:\r?\n)?$", line)
            visible_lines.append(_masked_markdown_line(line))
            if (
                closing
                and closing.group(1)[0] == fence_char
                and len(closing.group(1)) >= fence_length
            ):
                fence_char = ""
                fence_length = 0
            offset += len(line)
            continue

        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*?)(?:\r?\n)?$", line)
        if opening and not (
            opening.group(1)[0] == "`" and "`" in opening.group(2)
        ):
            fence_char = opening.group(1)[0]
            fence_length = len(opening.group(1))
            visible_lines.append(_masked_markdown_line(line))
            offset += len(line)
            continue

        visible_lines.append(line)
        heading_match = re.match(
            r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*(?:\r?\n)?$", line,
        )
        if heading_match:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading_match.group(2)).strip()
            if title:
                level = len(heading_match.group(1))
                line_end = offset + len(line.rstrip("\r\n"))
                heading_rows.append(
                    (f"{'#' * level} {title}", level, offset, line_end, offset + len(line))
                )
        offset += len(line)

    headings = tuple(
        _ReportMarkdownHeading(
            canonical=canonical,
            level=level,
            start=start,
            line_end=line_end,
            content_start=content_start,
            content_end=(
                heading_rows[index + 1][2]
                if index + 1 < len(heading_rows)
                else len(text)
            ),
        )
        for index, (canonical, level, start, line_end, content_start) in enumerate(
            heading_rows
        )
    )
    return _ParsedReportMarkdown(
        source=text,
        visible_text="".join(visible_lines),
        headings=headings,
    )


def _sub_visible_report_text(text: str, pattern: str, replacement: str) -> str:
    parsed = _parse_report_markdown(text)
    matches = list(re.finditer(pattern, parsed.visible_text))
    for match in reversed(matches):
        text = text[:match.start()] + match.expand(replacement) + text[match.end():]
    return text


def _normalize_report_evidence_coverage(text: str, candidate: dict) -> str:
    coverage = candidate.get("evidence_coverage")
    heading = "## 三、裁判结论"
    parsed = _parse_report_markdown(text)
    if coverage is None or not parsed.sections(heading):
        return text
    authoritative = f"**证据覆盖率：{round(float(coverage) * 100, 1)}%**"
    without_model_fields = _sub_visible_report_text(
        text, _report_labeled_field_pattern("证据覆盖率"), "",
    )
    without_model_fields = _sub_visible_report_text(
        without_model_fields, r"([：:])\s*[，、；;]+", r"\1",
    )
    without_model_fields = _sub_visible_report_text(
        without_model_fields, r"([，、；;])\s*[，、；;]+", r"\1",
    )
    normalized = _parse_report_markdown(without_model_fields)
    judge_sections = normalized.sections(heading)
    if not judge_sections:
        return without_model_fields
    section_start = judge_sections[0].line_end
    return (
        without_model_fields[:section_start]
        + "\n"
        + authoritative
        + without_model_fields[section_start:]
    )


def _effective_position_report_candidate(effective: dict) -> dict:
    """Build the sole position-action state consumed by report generation."""
    report_candidate = {
        **dict(effective or {}),
        "scale_plan": (effective or {}).get("effective_scale_plan") or [],
    }
    if report_candidate.get("action") != "exit":
        report_candidate["current_stop"] = (effective or {}).get("effective_stop")
        report_candidate["current_target"] = (effective or {}).get("effective_target")
    return report_candidate


def _bounded_report_scalar(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:240]
    return None


def _verdict_report_candidate(candidate: dict) -> dict:
    """Keep only finalized machine fields needed to render and validate a verdict."""
    result = {}
    for key in _VERDICT_REPORT_CANDIDATE_SCALARS:
        if key not in candidate:
            continue
        value = _bounded_report_scalar(candidate.get(key))
        if value is not None or candidate.get(key) is None:
            result[key] = value
    counted_ids = candidate.get("counted_evidence_ids")
    if isinstance(counted_ids, list):
        result["counted_evidence_ids"] = [
            str(item)[:240] for item in counted_ids[:6] if item is not None
        ]
    features = candidate.get("features")
    if isinstance(features, dict):
        result["features"] = {
            key: value
            for key in _VERDICT_REPORT_FEATURE_FIELDS
            if key in features
            and (
                (value := _bounded_report_scalar(features.get(key))) is not None
                or features.get(key) is None
            )
        }
    return result


def _decision_policy_report_summary(decision_policy: dict) -> dict:
    """Expose deterministic policy outputs without duplicating raw claim collections."""
    result = {}
    for key in _DECISION_POLICY_REPORT_SCALARS:
        if key not in decision_policy:
            continue
        value = _bounded_report_scalar(decision_policy.get(key))
        if value is not None or decision_policy.get(key) is None:
            result[key] = value
    scores = decision_policy.get("dimension_scores")
    if isinstance(scores, dict):
        result["dimension_scores"] = {
            key: value
            for key in _DECISION_DIMENSIONS
            if key in scores
            and (
                (value := _bounded_report_scalar(scores.get(key))) is not None
                or scores.get(key) is None
            )
        }
    return result


def _adaptive_report_heading_issues(parsed: _ParsedReportMarkdown) -> list[str]:
    """Validate exact adaptive heading lines once and in contract order."""
    headings = [heading.canonical for heading in parsed.headings]

    issues: list[str] = []
    unexpected = [
        heading for heading in headings if heading not in _VERDICT_REPORT_SECTIONS
    ]
    for heading in dict.fromkeys(unexpected):
        issues.append(f"存在未授权章节：{heading}")
    counts = {section: headings.count(section) for section in _VERDICT_REPORT_SECTIONS}
    for section, count in counts.items():
        label = section.removeprefix("## ").removeprefix("# ")
        if count == 0:
            issues.append(f"缺少必需章节：{label}")
        elif count > 1:
            issues.append(f"重复必需章节：{label}")
    if all(count == 1 for count in counts.values()):
        required_headings = [
            heading for heading in headings if heading in _VERDICT_REPORT_SECTIONS
        ]
        if required_headings != list(_VERDICT_REPORT_SECTIONS):
            issues.append("必需章节标题顺序不一致")
    return issues


def _directional_bullet_has_counted_provenance(
    bullet: str,
    evidence_id: str,
    expected_stance: str,
    adaptive_report: dict,
) -> bool:
    evidence_selection = adaptive_report.get("evidence_selection")
    if not isinstance(evidence_selection, dict):
        return False
    excluded_ids = {
        str(item.get("evidence_id") or "")
        for item in evidence_selection.get("excluded") or []
        if isinstance(item, dict)
    }
    if evidence_id in excluded_ids:
        return False
    counted_claims = [
        item for item in evidence_selection.get("counted") or []
        if isinstance(item, dict)
        and str(item.get("evidence_id") or "") == evidence_id
        and str(item.get("stance") or "") == expected_stance
    ]
    canonical_bullets = {
        f"- [{evidence_id}] {str(item.get('inference') or '').strip()}"
        for item in counted_claims
        if str(item.get("inference") or "").strip()
    }
    return bullet.strip() in canonical_bullets


def _validate_final_report(
    report: str,
    kind: str,
    candidate: dict,
    *,
    adaptive_report: dict | None = None,
) -> list[str]:
    """Return deterministic issues that prevent a model report from being published."""
    text = str(report or "").strip()
    issues: list[str] = []
    parsed_markdown = None
    validation_text = text
    if kind == "verdict" and adaptive_report is not None:
        parsed_markdown = _parse_report_markdown(text)
        validation_text = parsed_markdown.visible_text
        issues.extend(_adaptive_report_heading_issues(parsed_markdown))
    else:
        required_sections = (
            _POSITION_REPORT_SECTIONS
            if kind == "position_action"
            else _LEGACY_VERDICT_REPORT_SECTIONS
        )
        for section in required_sections:
            if section not in text:
                issues.append(
                    f"缺少必需章节：{section.removeprefix('## ').removeprefix('# ')}"
                )
    found_process = [marker for marker in _REPORT_PROCESS_MARKERS if marker in validation_text]
    if found_process:
        issues.append("包含过程性措辞：" + "、".join(found_process))
    if len(text) < 300:
        issues.append("正式报告过短（少于 300 字符）")

    if kind == "verdict":
        verdict = str(candidate.get("verdict") or "")
        confidence = candidate.get("confidence")
        verdict_values = _report_labeled_values(validation_text, "判断")
        confidence_values = _report_labeled_values(validation_text, "置信度")
        if verdict and verdict not in verdict_values:
            issues.append("判断与结构化结果不一致")
        if verdict and any(value != verdict for value in verdict_values):
            issues.append("存在冲突的判断")
        expected_confidence = f"{confidence}/10" if confidence is not None else ""
        if confidence is not None and expected_confidence not in confidence_values:
            issues.append("置信度与结构化结果不一致")
        if confidence is not None and any(
            value != expected_confidence for value in confidence_values
        ):
            issues.append("存在冲突的置信度")
        all_confidence_claims = [
            float(value) for value in re.findall(
                r"(?:最终)?置信度[：:]\s*(\d+(?:\.\d+)?)\s*/\s*10", validation_text,
            )
        ]
        if confidence is not None and any(value != float(confidence) for value in all_confidence_claims):
            if "存在冲突的置信度" not in issues:
                issues.append("存在冲突的置信度")
        if candidate.get("evidence_coverage") is not None:
            expected_coverage = round(float(candidate["evidence_coverage"]) * 100, 1)
            judge_text = (
                parsed_markdown.section_text("## 三、裁判结论")
                if parsed_markdown is not None else validation_text
            )
            _validate_labeled_number(judge_text, "证据覆盖率", expected_coverage, issues)
        if candidate.get("net_hardness") is not None:
            judge_text = (
                parsed_markdown.section_text("## 三、裁判结论")
                if parsed_markdown is not None else validation_text
            )
            _validate_labeled_number(judge_text, "净硬度", candidate["net_hardness"], issues)
        allowed_evidence_ids = set(candidate.get("counted_evidence_ids") or [])
        if candidate.get("evidence_coverage") is not None:
            headings = ("## 一、多头论点", "## 二、空头论点", "## 三、裁判结论")
            for index, heading in enumerate(headings[:2]):
                expected_stance = "bull" if index == 0 else "bear"
                if parsed_markdown is not None:
                    section = parsed_markdown.section_text(heading)
                else:
                    start = text.find(heading)
                    end = text.find(headings[index + 1], start + len(heading)) if start >= 0 else -1
                    section = text[start + len(heading):end] if start >= 0 and end >= 0 else ""
                content_lines = [line.strip() for line in section.splitlines() if line.strip()]
                bullets = [line for line in content_lines if line.startswith("-")]
                if len(bullets) != len(content_lines):
                    issues.append(f"{heading}存在未按 evidence_id 列表呈现的论点")
                if len(bullets) > 3:
                    issues.append(f"{heading}超过 3 条证据")
                for bullet in bullets:
                    ids = re.findall(r"\[((?:ev_|sys_)[^\]]+)\]", bullet)
                    if len(ids) != 1 or ids[0] not in allowed_evidence_ids:
                        issues.append(f"{heading}包含未计分或未标注 evidence_id 的证据")
                        break
                    if adaptive_report is not None and not _directional_bullet_has_counted_provenance(
                        bullet, ids[0], expected_stance, adaptive_report,
                    ):
                        issues.append(f"{heading}方向论点与计入证据来源不一致")
                        break
        all_verdict_claims = re.findall(
            r"(?:最终)?判断[：:]\s*(看多|偏多|观望偏多|中性|观望偏空|偏空|看空)", validation_text,
        )
        if verdict and any(value != verdict for value in all_verdict_claims):
            if "存在冲突的判断" not in issues:
                issues.append("存在冲突的判断")
        if verdict in BULLISH_VERDICTS:
            entry_low = candidate.get("entry_low") or candidate.get("entry")
            entry_high = candidate.get("entry_high") or candidate.get("entry")
            entry_values = _report_labeled_values(validation_text, "入场")
            entry_numbers = [_report_number(part) for value in entry_values for part in re.findall(r"-?\d+(?:\.\d+)?", value)]
            expected_entry = [_report_number(entry_low), _report_number(entry_high)]
            if entry_low is not None and (len(entry_values) != 1 or entry_numbers != expected_entry):
                issues.append("入场区间与结构化结果不一致")
            for field, label in (
                ("stop_loss", "止损"), ("target", "目标"),
                ("position_size_pct", "建议仓位"),
            ):
                value = candidate.get(field)
                _validate_labeled_number(validation_text, label, value, issues)
    elif kind == "position_action":
        action = str(candidate.get("action") or "")
        action_values = _report_labeled_values(text, "当前动作")
        if action and action not in action_values:
            issues.append("当前动作与结构化结果不一致")
        if action and any(value != action for value in action_values):
            issues.append("存在冲突的当前动作")
        immediate_actions = {
            "加仓": "add", "减仓": "trim", "清仓": "exit", "退出": "exit", "持有": "hold",
        }
        for word in re.findall(r"(?:当前立即|现在立即|立即)(加仓|减仓|清仓|退出|持有)", text):
            if immediate_actions[word] != action:
                issues.append("当前指令与结构化动作不一致")
                break
        fields = [
            ("add_shares", "加仓股数"), ("trim_shares", "减仓股数"),
            ("trim_pct", "减仓比例"),
        ]
        if action != "exit":
            fields.extend((
                ("current_stop", "当前有效止损"),
                ("current_target", "当前有效目标"),
            ))
        for field, label in fields:
            value = candidate.get(field)
            _validate_labeled_number(text, label, value, issues)
        plan_section = text.split("## 条件触发计划", 1)[-1].split("\n## ", 1)[0]
        parsed_plan = []
        for item_action, trigger, shares, pct, new_stop in re.findall(
            r"(?m)^\s*-\s*(add|trim)\s*@\s*(\d+(?:\.\d+)?)[，,]\s*"
            r"(?:(\d+)\s*股|比例\s*(\d+(?:\.\d+)?))"
            r"(?:[，,]\s*新止损\s*(\d+(?:\.\d+)?))?",
            plan_section,
        ):
            try:
                parsed_level = (
                    item_action, float(trigger), "shares" if shares else "pct",
                    int(shares) if shares else float(pct),
                    float(new_stop) if new_stop else None,
                )
                if (
                    not math.isfinite(parsed_level[1])
                    or not math.isfinite(parsed_level[3])
                    or (parsed_level[4] is not None and not math.isfinite(parsed_level[4]))
                ):
                    raise ValueError("条件触发计划数值必须有限")
            except (TypeError, ValueError, OverflowError):
                parsed_plan = None
                break
            parsed_plan.append(parsed_level)
        expected_plan = []
        for level in candidate.get("scale_plan") or []:
            try:
                if not isinstance(level, dict):
                    raise TypeError("scale_plan 档位必须是对象")
                has_shares = level.get("shares") is not None
                has_pct = level.get("pct") is not None
                if has_shares == has_pct:
                    raise ValueError("shares/pct 必须且只能提供一个")
                expected_level = (
                    str(level["action"]), float(level["trigger_price"]),
                    "shares" if has_shares else "pct",
                    int(level["shares"]) if has_shares else float(level["pct"]),
                    float(level["new_stop"]) if level.get("new_stop") is not None else None,
                )
                if (
                    not math.isfinite(expected_level[1])
                    or not math.isfinite(expected_level[3])
                    or (expected_level[4] is not None and not math.isfinite(expected_level[4]))
                ):
                    raise ValueError("scale_plan 数值必须有限")
                expected_plan.append(expected_level)
            except (KeyError, TypeError, ValueError, OverflowError):
                issues.append("结构化条件触发计划字段无效")
                expected_plan = None
                break
        if parsed_plan != expected_plan:
            issues.append(
                "条件触发计划与结构化结果不一致："
                f"expected={expected_plan!r}; parsed={parsed_plan!r}"
            )
        if candidate.get("ladder_intent") == "clear":
            if "条件触发计划已清空" not in plan_section:
                issues.append("ladder_intent=clear 时必须明确说明条件触发计划已清空")
            if parsed_plan:
                issues.append("ladder_intent=clear 时条件触发计划不得包含档位")
    return issues


def _mx_news_evidence_type(title: str, content: str) -> str:
    """Keep navigation/sidebar text from turning ordinary investor Q&A into a material event."""
    ordinary_qa = ("股东总户数", "股东户数", "答投资者问", "互动平台回答")
    if any(marker in title for marker in ordinary_qa):
        return classify_evidence_type(title, default="general")
    return classify_evidence_type(f"{title} {content}", default="general")


def _normalize_research_gaps(
    gaps: list[dict], *, held: bool, safety_scan_status: str,
) -> list[dict]:
    """Unknown price-move attribution is reportable, but alone cannot block a held-position plan."""
    normalized = []
    price_markers = ("暴跌", "大跌", "下跌原因", "暴涨", "大涨", "上涨原因")
    no_event_markers = ("无明确利空公告", "未发现明确利空", "无明确公告", "原因未明")
    for raw in gaps:
        gap = dict(raw)
        description = str(gap.get("description") or "")
        if (held and safety_scan_status == "clear" and gap.get("severity") == "critical"
                and any(marker in description for marker in price_markers)
                and any(marker in description for marker in no_event_markers)):
            gap["severity"] = "noncritical"
        normalized.append(gap)
    return normalized


def _tool_evidence(name: str, raw: str, ts_code: str) -> list[dict]:
    """Convert accepted tool output into the compact evidence ledger."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(parsed, dict) and parsed.get("error"):
        return []
    if name == "web_search" and isinstance(parsed, dict):
        items = []
        query_category = str(parsed.get("category") or "")
        for row in parsed.get("results") or []:
            if not row.get("entity_matched") or int(row.get("source_tier") or 3) > 3:
                continue
            # 按内容归类而非查询 category：general 搜索命中的警示函归 regulatory，
            # 否则交叉验证（按 evidence_type 分桶）会把同事实的印证拆到不同桶。
            row_text = f"{row.get('title') or ''} {row.get('snippet') or ''}"
            items.append(make_evidence_item(
                # title+snippet 都进 fact：目标价等关键数值常在 snippet 里，
                # 只取 title 会让独立复核无法验证候选引用的数值。
                fact=row_text.strip()[:300],
                inference="待 Agent 结合其他证据评估",
                evidence_type=classify_evidence_type(
                    row_text, preferred=query_category, default=query_category or "general",
                ),
                tool_name=name,
                source=row,
            ))
        return [_with_policy_metadata(item, name) for item in items]
    if name == "mx_news_search" and isinstance(parsed, dict):
        items, seen = [], set()
        stock_name = data.get_name_map().get(ts_code) or ""
        for row in parsed.get("results") or []:
            title = str(row.get("title") or "").strip()
            content = " ".join(str(row.get("content") or "").split())
            entity_text = " ".join((title, content, str(row.get("entity") or "")))
            fingerprint = (title, content[:160])
            if (not title or fingerprint in seen
                    or not entity_matches(entity_text, ts_code, stock_name)):
                continue
            seen.add(fingerprint)
            fact = f"{title} {content}".strip()[:700]
            items.append(make_evidence_item(
                fact=fact,
                inference="待 Agent 结合其他证据评估",
                evidence_type=_mx_news_evidence_type(title, content),
                tool_name=name,
                source={
                    **row, "site": row.get("institution") or "妙想财经",
                    "source_tier": source_tier(
                        str(row.get("url") or ""), str(row.get("institution") or ""),
                    ),
                    "entity_matched": True,
                    "freshness_status": "current",
                },
            ))
            if len(items) >= 8:
                break
        return [_with_policy_metadata(item, name) for item in items]
    summary = trace_mod.summarize_tool_result(name, raw)
    if not isinstance(summary, dict) or summary.get("error") or summary.get("note"):
        return []
    fact = json.dumps(summary, ensure_ascii=False, sort_keys=True)[:800]
    return [_with_policy_metadata(make_evidence_item(
        fact=fact,
        inference="结构化数据，供多空论证使用",
        # 妙想等返回的公告/业绩内容按内容归入对应 material 桶（tier 1），
        # 无类别词的纯行情/估值 JSON 保持 structured_data。
        evidence_type=classify_evidence_type(fact, default="structured_data"),
        tool_name=name,
        source={
            "title": name, "site": name, "url": "", "date": date.today().isoformat(),
            "source_tier": 1, "entity_matched": True, "freshness_status": "current",
        },
    ), name)]


def _with_policy_metadata(item: dict, tool_name: str) -> dict:
    """Attach backend-owned claim metadata before evidence reaches the model."""
    evidence_type = str(item.get("evidence_type") or "")
    lower_tool = tool_name.lower()
    if any(marker in lower_tool for marker in ("moneyflow", "money_flow", "northbound", "dragon_tiger")):
        dimension = "capital"
    elif any(marker in lower_tool for marker in ("daily_price", "realtime", "technical")):
        dimension = "technical"
    elif evidence_type == "money_flow":
        dimension = "capital"
    elif evidence_type in {"industry", "market_sentiment"}:
        dimension = "sentiment"
    else:
        dimension = "fundamental"
    is_event = evidence_type not in {"structured_data", "money_flow", "market_sentiment"}
    as_of = item.get("as_of") or item.get("published_at") or date.today().isoformat()
    return {
        **item,
        "dimension": dimension,
        "nature": "fact" if is_event else "current",
        "as_of": as_of,
        "frequency": item.get("frequency") or ("event" if is_event else "daily"),
        "is_complete": bool(item.get("is_complete", True)),
        "independence_group": item.get("independence_group") or item["id"],
    }


def _run_langgraph_loop(
    *, ts_code: str, client, model: str, messages: list, max_iter: int, emit,
    system_context: str = "",
    report_context: dict | None = None,
    finalize_candidate: Callable[[str, dict], tuple[dict, dict]] | None = None,
    prepare_candidate: Callable[[str, dict, list[dict]], tuple[dict, dict]] | None = None,
    initial_evidence: list[dict] | None = None,
    position_baseline: dict | None = None,
) -> dict:
    """Run the sole model/tool orchestration path as a LangGraph StateGraph."""
    authoritative_report_context = deepcopy(report_context or {})
    controller = EvidenceController()
    controller.add_evidence(list(initial_evidence or []))
    stock_name = data.get_name_map().get(ts_code) or ""

    def parse_review_json(content: str) -> dict:
        cleaned = (content or "").strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```json").removeprefix("```")
            cleaned = cleaned.removesuffix("```").strip()
        return json.loads(cleaned or "{}")

    def prepare(state):
        emit({"type": "status", "stage": "preparing", "message": "正在准备分析上下文"})
        return {
            "messages": list(messages), "analysis_text": "", "model_iterations": 0,
            "research_rounds": 0, "evidence": list(controller.evidence.values()), "gaps": [],
            "position_baseline": deepcopy(position_baseline or {}), "draft_proposal": {},
        }

    def safety_scan(state):
        emit({"type": "status", "stage": "safety_scan", "message": "正在执行权威黑天鹅扫描"})
        query = (
            f"{stock_name} {ts_code.split('.')[0]} {ts_code} "
            "重大公告 监管 立案 处罚 诉讼 停牌 业绩预警"
        )
        with observability.tool_trace("authoritative_scan", {
            "ts_code": ts_code,
            "name": stock_name,
            "query": query,
            "freshness": "oneYear",
            "count": 10,
        }) as tool_span:
            try:
                raw = data.web_search(
                    ts_code, category="general", name=stock_name, query=query,
                    freshness="oneYear", count=10,
                )
                try:
                    parsed = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    parsed = {"error": "返回不可解析"}
            except Exception as exc:
                raw = json.dumps({"error": str(exc)}, ensure_ascii=False)
                parsed = {"error": str(exc)}
            tool_span.set_outputs({"result": raw})
        evidence = [item for item in _tool_evidence("web_search", raw, ts_code)
                    if int(item.get("source_tier") or 3) <= 2]
        # 扫描执行成功即成功，不要求 Tier 1 命中。旧逻辑把"只搜到 Tier 2"
        # 误判为扫描失败（safety_scan_status=unknown -> 阻断收尾），且丢掉
        # 已发现的风险事件（601872 双源警示函被整条丢弃）。事件是否需交叉
        # 验证由 evidence_control 的 material 桶门控负责。
        success = _safety_scan_outcome(parsed)
        controller.record_safety_scan(success=success, evidence=evidence)
        if not success:
            detail = str(parsed.get("error") or "权威数据服务返回异常")
            controller.failures.append(f"authoritative_scan: {detail}")
        emit({
            "type": "tool_result", "iteration": 0, "tool_call_id": "safety-scan",
            "name": "authoritative_scan", "summary": trace_mod.summarize_web_search(raw),
            "raw": raw,
        })
        update = {
            "safety_scan_status": controller.safety_scan_status,
            "evidence": list(controller.evidence.values()),
        }
        if not success:
            update.update({
                "outcome_reason": "provider_failure",
                "next_actions": ["权威数据服务恢复后重新运行分析"],
                "failures": list(controller.failures),
                "research_metrics": controller.research_metrics(
                    stop_reason="authoritative_scan_failure"
                ),
            })
        return update

    # LLM usage 跟踪：ark 网关超上下文会报错或静默截断，peak 不记录就无从感知。
    usage_stats = {"peak_prompt_tokens": 0, "total_completion_tokens": 0, "calls": 0}

    def _record_usage(response, *, call: str, iteration: int) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        usage_stats["peak_prompt_tokens"] = max(usage_stats["peak_prompt_tokens"], prompt_tokens)
        usage_stats["total_completion_tokens"] += completion_tokens
        usage_stats["calls"] += 1
        emit({
            "type": "usage", "call": call, "iteration": iteration,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "peak_prompt_tokens": usage_stats["peak_prompt_tokens"],
        })

    def reason(state):
        iteration = int(state.get("model_iterations", 0))
        final_assessment = bool(state.get("final_assessment_requested"))
        status_event = {
            "type": "status", "stage": "assessing" if final_assessment else "researching",
            "message": "补证预算已用尽，正在最终评估" if final_assessment else "正在分析并按缺口补证",
        }
        if not final_assessment:
            status_event.update({
                "current": controller.research_rounds + 1,
                "total": controller.config.max_research_rounds,
            })
        emit(status_event)
        reason_messages = list(state.get("messages") or [])
        reason_tools = TOOLS
        tool_choice = None
        if final_assessment:
            reason_messages.append({
                "role": "user",
                "content": (
                    "补证预算已用尽。不得再搜索；必须基于已有证据重新评估每个 gap，"
                    "将已被证据覆盖的 gap 关闭，将不影响持仓风险管理的未知项降为 noncritical，"
                    "然后只调用 submit_research_state 提交最终评估。"
                ),
            })
            reason_tools = [next(
                tool for tool in TOOLS if tool["function"]["name"] == "submit_research_state"
            )]
            tool_choice = {
                "type": "function", "function": {"name": "submit_research_state"},
            }
        try:
            request = {
                "model": model, "messages": reason_messages, "tools": reason_tools,
                "max_tokens": 16384, "temperature": 0.4,
                "extra_body": {"thinking": {"type": "disabled"}},
            }
            if tool_choice:
                request["tool_choice"] = tool_choice
            response = client.chat.completions.create(**request)
        except Exception as exc:
            controller.failures.append(f"model: {exc}")
            return {
                "pending_tools": [], "outcome_reason": "provider_failure",
                "next_actions": ["模型服务恢复后重新运行分析"],
                "failures": list(controller.failures),
            }
        _record_usage(response, call="reason", iteration=iteration)
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise AnalysisError(f"AI exceeded token limit at iteration {iteration}")
        msg = choice.message
        if msg.content:
            emit({"type": "assistant_text", "iteration": iteration, "content": msg.content})
        return {
            "messages": [*(state.get("messages") or []), msg],
            "assistant_message": msg,
            "pending_tools": list(msg.tool_calls or []),
            "model_iterations": iteration + 1,
            "final_assessment_done": final_assessment,
        }

    def _candidate_blockers(name: str, tool_input: dict) -> list[str]:
        action = tool_input.get("action") if name == "record_position_action" else None
        blockers = list(controller.finalization_decision(action=action).blockers)
        if name == "record_verdict" and tool_input.get("verdict") in BULLISH_VERDICTS:
            blockers.extend(
                f"{field} 必须是大于 0 的具体价格"
                for field in ("entry", "stop_loss", "target")
                if not isinstance(tool_input.get(field), (int, float)) or tool_input.get(field) <= 0
            )
        if (name == "record_verdict"
                and tool_input.get("growth_valuation_mode") in {"required", "mixed"}
                and tool_input.get("verdict") in BEARISH_VERDICTS
                and tool_input.get("valuation_basis") in (None, "static_pe_only")):
            blockers.append("成长成员分已启用估值约束，偏空不得仅使用静态 PE")
        if name == "record_verdict" and _valuation_basis_conflict(tool_input, controller.thesis):
            blockers.append("空头论点明确使用估值依据，valuation_basis 不得填 non_valuation")
        try:
            from apex import watchlist as _wl_graph
            held = any(p.get("ts_code") == ts_code for p in _wl_graph.load().get("active_positions", []))
        except Exception:
            held = False
        if name == "record_verdict" and held:
            blockers.append("已持仓票必须提交 position_action")
        return blockers

    def execute_tools(state):
        appended = list(state.get("messages") or [])
        draft_kind = state.get("draft_kind") or ""
        draft_data = dict(state.get("draft_data") or {})
        draft_proposal = dict(state.get("draft_proposal") or {})
        finalization_metadata = dict(state.get("finalization_metadata") or {})
        for tool_call in state.get("pending_tools") or []:
            name = tool_call.function.name
            try:
                tool_input = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                tool_input = {}
            tool_message_content = ""
            emit({
                "type": "tool_call", "iteration": state.get("model_iterations", 1) - 1,
                "tool_call_id": tool_call.id, "name": name, "args": tool_input,
            })

            with observability.tool_trace(name, tool_input) as tool_span:
                if name == "submit_research_state":
                    try:
                        from apex import watchlist as _wl_assessment
                        held = any(
                            p.get("ts_code") == ts_code
                            for p in _wl_assessment.load().get("active_positions", [])
                        )
                    except Exception:
                        held = False
                    controller.submit_assessment(
                        thesis=str(tool_input.get("thesis") or ""),
                        gaps=_normalize_research_gaps(
                            list(tool_input.get("gaps") or []), held=held,
                            safety_scan_status=controller.safety_scan_status,
                        ),
                        ready=bool(tool_input.get("ready")),
                        next_actions=list(tool_input.get("next_actions") or []),
                        count_research_round=not bool(state.get("final_assessment_done")),
                    )
                    result = json.dumps({
                        "accepted": True,
                        "stop": controller.should_stop().stop,
                        "finalization": controller.finalization_decision().allowed,
                    }, ensure_ascii=False)
                elif name in {"record_verdict", "record_position_action"}:
                    if name == "record_verdict" and prepare_candidate is not None:
                        try:
                            tool_input, policy_metadata = prepare_candidate(
                                "verdict", tool_input, list(controller.evidence.values()),
                            )
                            finalization_metadata.update(policy_metadata)
                        except (EvidenceClaimError, ValueError) as exc:
                            result = json.dumps({"error": str(exc)}, ensure_ascii=False)
                            tool_span.set_outputs({"result": result})
                            appended.append({"role": "tool", "tool_call_id": tool_call.id, "content": result})
                            continue
                    blockers = _candidate_blockers(name, tool_input)
                    proposal, effective = {}, {}
                    if name == "record_position_action":
                        current_price = None
                        try:
                            current_price = (data.get_realtime_price([ts_code]) or {}).get(ts_code)
                        except Exception:
                            pass
                        proposal, effective, position_blockers = _prepare_position_action_candidate(
                            tool_input, dict(state.get("position_baseline") or {}), ts_code, current_price,
                        )
                        blockers.extend(position_blockers)
                    if blockers:
                        result = json.dumps({"error": "；".join(blockers)}, ensure_ascii=False)
                    else:
                        draft_kind = "verdict" if name == "record_verdict" else "position_action"
                        draft_data = effective if name == "record_position_action" else tool_input
                        draft_proposal = proposal if name == "record_position_action" else {}
                        result = "candidate recorded; pending independent review"
                else:
                    stop = controller.should_stop()
                    if stop.stop:
                        result = json.dumps({"error": f"动态预算已停止: {stop.reason}"}, ensure_ascii=False)
                    else:
                        try:
                            result = _dispatch_tool(name, tool_input)
                            parsed = json.loads(result) if isinstance(result, str) else result
                            success = not (isinstance(parsed, dict) and parsed.get("error"))
                            controller.record_external_call(
                                name, success=success,
                                error=str(parsed.get("error") or "") if isinstance(parsed, dict) else "",
                            )
                            new_evidence = _tool_evidence(name, result, ts_code)
                            controller.add_evidence(new_evidence)
                            if new_evidence:
                                tool_message_content = (
                                    str(result)
                                    + "\n\nEVIDENCE_LEDGER_IDS="
                                    + json.dumps([
                                        {
                                            "evidence_id": item.get("id"),
                                            "fact": item.get("fact"),
                                            "source_tier": item.get("source_tier"),
                                            "dimension": item.get("dimension"),
                                            "nature": item.get("nature"),
                                            "as_of": item.get("as_of"),
                                            "frequency": item.get("frequency"),
                                            "is_complete": item.get("is_complete"),
                                            "independence_group": item.get("independence_group"),
                                        }
                                        for item in new_evidence
                                    ], ensure_ascii=False)
                                )
                        except Exception as exc:
                            controller.record_external_call(name, success=False, error=str(exc))
                            result = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    emit({
                        "type": "tool_result", "iteration": state.get("model_iterations", 1) - 1,
                        "tool_call_id": tool_call.id, "name": name,
                        "summary": trace_mod.summarize_tool_result(name, result), "raw": result,
                    })
                tool_span.set_outputs({"result": result})
            appended.append({
                "role": "tool", "tool_call_id": tool_call.id,
                "content": tool_message_content or result,
            })
        return {
            "messages": appended, "pending_tools": [],
            "draft_kind": draft_kind, "draft_data": draft_data, "draft_proposal": draft_proposal,
            "finalization_metadata": finalization_metadata,
            "evidence": list(controller.evidence.values()),
            "gaps": list(controller.gaps), "research_rounds": controller.research_rounds,
        }

    def assess(state):
        emit({"type": "status", "stage": "assessing", "message": "正在判断证据是否收敛"})
        if state.get("outcome_reason"):
            return {"route": "abstain"}
        if state.get("draft_kind") and state.get("draft_data"):
            return {"route": "draft"}
        if controller.finalization_decision().allowed:
            return {"route": "draft"}
        stop = controller.should_stop()
        if (stop.stop and stop.reason in {"external_call_budget", "research_round_budget", "no_progress"}
                and not state.get("final_assessment_done")):
            return {"route": "research", "final_assessment_requested": True}
        if stop.stop or int(state.get("model_iterations", 0)) >= max_iter:
            blockers = controller.finalization_decision().blockers
            business_unknowns = [
                item for item in blockers
                if item not in {"Agent 尚未声明证据收敛", "复核返工尚未取得新增证据并重新评估"}
            ]
            reason = "evidence_gap" if business_unknowns else (
                "model_iteration_exhausted"
                if not stop.stop else "research_budget_exhausted"
            )
            return {
                "route": "abstain", "unknowns": business_unknowns,
                "outcome_reason": reason,
                "next_actions": list(controller.next_actions) or (
                    ["补齐关键证据后重新运行分析"] if business_unknowns else ["重新运行分析"]
                ),
                "research_metrics": controller.research_metrics(
                    stop_reason=stop.reason if stop.stop else "model_iteration_budget"
                ),
            }
        return {"route": "research"}

    def draft(state):
        if state.get("draft_kind") and state.get("draft_data"):
            kind = str(state.get("draft_kind"))
            candidate = dict(state.get("draft_data") or {})
            return {
                "draft_route": "review",
            }
        emit({"type": "status", "stage": "drafting", "message": "证据已收敛，正在生成结构化结论"})
        try:
            from apex import watchlist as _wl_draft
            held = any(p.get("ts_code") == ts_code for p in _wl_draft.load().get("active_positions", []))
        except Exception:
            held = False
        tool_name = "record_position_action" if held else "record_verdict"
        tool = next(item for item in TOOLS if item["function"]["name"] == tool_name)
        failure = ""
        for attempt in range(2):
            if attempt:
                emit({"type": "draft_retry", "reason": failure, "attempt": attempt + 1})
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[*(state.get("messages") or []), {
                        "role": "user",
                        "content": (
                            "证据门控已通过。现在必须基于已有上下文提交结构化最终结论，"
                            f"只调用 {tool_name}，不得继续搜索或输出额外正文。"
                            + (
                                "上一次提交被业务校验拒绝，必须逐条修正以下问题："
                                f"{failure}"
                                if attempt else ""
                            )
                        ),
                    }],
                    tools=[tool],
                    tool_choice={"type": "function", "function": {"name": tool_name}},
                    max_tokens=16384, temperature=0.2 if attempt == 0 else 0,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                _record_usage(response, call="draft", iteration=int(state.get("model_iterations", 0)))
                calls = list(response.choices[0].message.tool_calls or [])
                if len(calls) != 1 or calls[0].function.name != tool_name:
                    failure = f"草稿未调用要求的 {tool_name}"
                    continue
                candidate = json.loads(calls[0].function.arguments or "{}")
                with observability.tool_trace(tool_name, candidate) as tool_span:
                    policy_metadata = dict(state.get("finalization_metadata") or {})
                    if tool_name == "record_verdict" and prepare_candidate is not None:
                        try:
                            candidate, prepared_metadata = prepare_candidate(
                                "verdict", candidate, list(controller.evidence.values()),
                            )
                            policy_metadata.update(prepared_metadata)
                        except (EvidenceClaimError, ValueError) as exc:
                            failure = str(exc)
                            tool_span.set_outputs({"result": {"error": failure}})
                            continue
                    blockers = _candidate_blockers(tool_name, candidate)
                    proposal, effective = {}, {}
                    if tool_name == "record_position_action":
                        current_price = None
                        try:
                            current_price = (data.get_realtime_price([ts_code]) or {}).get(ts_code)
                        except Exception:
                            pass
                        proposal, effective, position_blockers = _prepare_position_action_candidate(
                            candidate, dict(state.get("position_baseline") or {}), ts_code, current_price,
                        )
                        blockers.extend(position_blockers)
                    if blockers:
                        failure = "；".join(blockers)
                        tool_span.set_outputs({"result": {"error": failure}})
                        continue
                    result = {
                        "draft_kind": "position_action" if held else "verdict",
                        "draft_data": effective if tool_name == "record_position_action" else candidate,
                        "draft_proposal": proposal if tool_name == "record_position_action" else {},
                        "finalization_metadata": policy_metadata,
                        "draft_route": "review",
                    }
                    tool_span.set_outputs({"result": result})
                    return result
            except Exception as exc:
                failure = str(exc)
                if isinstance(exc, (ConnectionError, TimeoutError)):
                    controller.failures.append(f"model: {exc}")
        controller.failures.append(f"draft: {failure}")
        provider_failure = any(item.startswith("model:") for item in controller.failures)
        return {
            "draft_route": "abstain",
            "outcome_reason": "provider_failure" if provider_failure else "model_iteration_exhausted",
            "next_actions": ["模型服务恢复后重新运行分析"] if provider_failure else ["重新运行分析并生成结构化结论"],
            "failures": list(controller.failures),
            "research_metrics": controller.research_metrics(stop_reason="draft_generation_failed"),
        }

    def review(state):
        emit({"type": "status", "stage": "reviewing", "message": "正在进行独立复核"})
        compact = {
            "candidate_kind": state.get("draft_kind"),
            "baseline": state.get("position_baseline") or {},
            "proposal": state.get("draft_proposal") or {},
            "effective": state.get("draft_data") or {},
            "evidence": state.get("evidence") or [],
            "gaps": state.get("gaps") or [],
            "safety_scan_status": state.get("safety_scan_status"),
            # 系统注入的确定性数据（行情/大盘/情绪/持仓计划/历史判断）。
            # 不给 reviewer 看就会把候选引用的大盘/持仓数据误判为"未经验证的假设"，
            # 强制 rework 且 agent 无法通过补搜修复 -> 白耗预算后弃权。
            "system_context": system_context,
        }
        review_system = (
            "你是独立审稿人。只检查 effective 结论是否被给定证据支持、是否存在重大未知。"
            "system_context 是系统注入的确定性数据（行情/技术/大盘/情绪/持仓计划/历史判断），"
            "视为已验证：effective 引用其中数据时不必要求外部证据。"
            "baseline 是冻结的历史持仓基线，proposal 是本次提交的拟议修改，effective 是唯一需判断的完整结果。"
            "proposal 只能用来解释 effective 相对 baseline 的变化；数值不同本身不是冲突。"
            "只有缺少调整依据、违反风险约束或 effective 内部互相矛盾时才记录问题。"
            "rework 仅用于影响结论方向的重大外部事实主张无支撑；"
            "技术指标等次要出入记入 issues 但不应单独导致 rework。"
            "不得补造事实。只输出 JSON: {outcome: pass|rework|abstain, "
            "issues: [{message: string, severity: minor|material, blocking: boolean}]}。"
        )
        review_user = json.dumps(compact, ensure_ascii=False)
        # 复核输出本应只有几百 token。temp=0 下模型偶发复读循环，一路写到
        # max_tokens 上限被硬截断，JSON 断在字符串中间 -> 解析失败 -> 误弃权
        # （600487: completion=16384 恰好等于上限，正常值 12~305）。一次解析
        # 失败不等于证据不足：换温度重试一次打破复读，仍失败才按复核失败处理。
        payload, failure = None, ""
        for attempt in range(2):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": review_system + (
                            " 输出务必极简：outcome 一个词，issues 每条一句话。"
                            if attempt else "")},
                        {"role": "user", "content": review_user},
                    ],
                    max_tokens=16384, temperature=0 if attempt == 0 else 0.3,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                _record_usage(response, call="review", iteration=int(state.get("model_iterations", 0)))
                content = response.choices[0].message.content or "{}"
                truncated = getattr(response.choices[0], "finish_reason", None) == "length"
                payload = parse_review_json(content)
                if truncated:
                    # 解析侥幸成功但输出被截断，结论可能不完整，同样重试。
                    failure = "复核输出被 max_tokens 截断"
                    continue
                break
            except Exception as exc:
                failure = str(exc)
                payload = None
        if payload is None:
            emit({"type": "review_retry_failed", "reason": failure})
            outcome, issues = "abstain", [f"独立复核失败: {failure}"]
            technical_failure = True
        else:
            outcome = str(payload.get("outcome") or "abstain")
            issues = list(payload.get("issues") or [])
            technical_failure = False
        revision_count = int(state.get("review_revision_count", 0))
        outcome, issues = _review_transition(
            outcome, issues, revision_count, technical_failure=technical_failure,
        )
        if outcome != "revise":
            outcome = controller.record_review(outcome, issues)
        emit({"type": "review", "outcome": outcome, "issues": issues})
        update = {"review_outcome": outcome, "review_issues": issues}
        if outcome == "revise":
            update.update({
                "draft_kind": "", "draft_data": {}, "draft_proposal": {},
                "review_revision_count": revision_count + 1,
                "messages": [*(state.get("messages") or []), {
                    "role": "user",
                    "content": "独立复核发现结论表达或数据口径矛盾。只修订结论，不新增事实：" + "；".join(issues),
                }],
            })
        if outcome == "abstain":
            technical_failure = any(issue.startswith("独立复核失败:") for issue in issues)
            controller.failures.extend(
                f"review: {issue}" for issue in issues
                if issue and f"review: {issue}" not in controller.failures
            )
            update.update({
                "outcome_reason": "review_failure",
                "next_actions": [
                    "独立复核服务恢复后重新运行分析"
                    if technical_failure else "修正候选结论后重新运行独立复核"
                ],
                "failures": list(controller.failures),
                "research_metrics": controller.research_metrics(stop_reason="review_failure"),
            })
        if outcome == "rework":
            update.update({
                "draft_kind": "", "draft_data": {}, "draft_proposal": {},
                "messages": [*(state.get("messages") or []), {
                    "role": "user", "content": "独立复核要求定向补证：" + "；".join(issues),
                }],
            })
        return update

    def report(state):
        emit({"type": "status", "stage": "reporting", "message": "正在生成正式分析报告"})
        kind = str(state.get("draft_kind") or "")
        candidate = dict(state.get("draft_data") or {})
        finalization_metadata: dict = dict(state.get("finalization_metadata") or {})
        if finalize_candidate is not None:
            candidate, late_metadata = finalize_candidate(kind, candidate)
            candidate = dict(candidate or {})
            finalization_metadata.update(dict(late_metadata or {}))
        structured_verdict = kind == "verdict" and bool(authoritative_report_context)
        if kind == "position_action":
            candidate = _effective_position_report_candidate(candidate)
            heading_contract = "\n".join(_POSITION_REPORT_SECTIONS)
            ladder_contract = (
                "每个 scale_plan 档必须写成 `- <action> @ <trigger_price>，<shares> 股`；"
                "若 trim 档按比例执行，则写成 `- trim @ <trigger_price>，比例 <pct>`。"
            )
            if any(
                isinstance(level, dict) and level.get("new_stop") is not None
                for level in candidate.get("scale_plan") or []
            ):
                ladder_contract += "有新止损的档位必须追加 `，新止损 <new_stop>`。"
            format_contract = (
                "持仓动作报告必须依次包含这些标题：\n"
                + heading_contract + "\n\n"
                "核心判断和当前持仓动作章节都必须逐字写出 `**当前动作：<action>**`。"
                "effective 中非空的当前动作字段必须使用这些标签逐字写出："
                "`**加仓股数：<add_shares>**`、`**减仓股数：<trim_shares>**`、"
                "`**减仓比例：<trim_pct>**`、`**当前有效止损：<current_stop>**`、"
                "`**当前有效目标：<current_target>**`。"
                + ladder_contract
                + "条件触发计划必须区分当前动作与未来条件，不得把未来 add/trim 写成现役指令。"
                + (
                    "ladder_intent=clear：必须写出“条件触发计划已清空”，且不得列出任何档位。"
                    if candidate.get("ladder_intent") == "clear" else ""
                )
            )
        else:
            verdict_sections = (
                _VERDICT_REPORT_SECTIONS
                if structured_verdict
                else _LEGACY_VERDICT_REPORT_SECTIONS
            )
            heading_contract = "\n".join(verdict_sections)
            format_contract = (
                "未持仓 verdict 报告必须依次包含这些标题：\n"
                + heading_contract + "\n\n"
                "核心判断必须逐字写出 `**判断：<verdict>**` 和 `**置信度：<confidence>/10**`。"
                "裁判结论必须逐字写出 `**净硬度：<net_hardness>**`。"
                "多空论点每条必须以 `- [<evidence_id>]` 开头，只能使用 confirmed_evidence 中的 ID，每边 0-3 条。"
                "看多类结论的操作建议必须逐字写出 `**入场：<entry_low>–<entry_high>**`、"
                "`**止损：<stop_loss>**`、`**目标：<target>**`、`**建议仓位：<position_size_pct>%**`。"
                + (
                    "新增详细章节的唯一权威来源是 `adaptive_report.market`、"
                    "`adaptive_report.history`、`adaptive_report.playstyle`、"
                    "`adaptive_report.evidence_selection` 和 `adaptive_report.unknowns`。"
                    "`evidence_selection.counted` 中的计入证据可以支持方向；"
                    "每条方向论点必须严格写成 `- [<evidence_id>] <counted inference>`，"
                    "整条只允许对应 counted inference 原文，不得追加评论、否定或其他主张，"
                    "并放入与 counted stance 一致的多头或空头章节。"
                    "被排除证据只能解释排除原因，不得作为多头或空头论点的 evidence_id，"
                    "也不得支持方向或硬度。"
                    "对应对象或列表为空时，只写一句简短的“无可靠数据”或“无历史样本”，"
                    "不得补写替代事实。"
                    "不得复制或推断先前 assistant 消息中的事实。"
                    "权威数据存在且充足时，目标为 2000–3000 个中文字符；"
                    "准确性与完整性优先于长度，不得为凑字数添加未验证事实。"
                    if structured_verdict else ""
                )
            )
        decision_policy_context = finalization_metadata.get("decision_policy") or {}
        counted_ids = {
            item.get("evidence_id")
            for item in decision_policy_context.get("counted_claims") or []
        }
        confirmed_evidence = [
            {
                key: item.get(key)
                for key in (
                    "id", "fact", "inference", "evidence_type", "source_name",
                    "source_url", "published_at", "source_tier", "freshness_status",
                    "as_of", "frequency", "is_complete", "independence_group",
                )
                if item.get(key) is not None
            }
            for item in (state.get("evidence") or [])
            if kind != "verdict" or not decision_policy_context or item.get("id") in counted_ids
        ]
        report_decision_policy = {
            key: value for key, value in decision_policy_context.items()
            if key != "excluded_claims"
        }
        adaptive_context = deepcopy(authoritative_report_context)
        if kind == "verdict":
            finalized_context = build_report_context(
                history_entries=[],
                market_context={},
                playstyle=finalization_metadata.get("playstyle"),
                playstyle_features={},
                playstyle_fit=finalization_metadata.get("playstyle_fit"),
                risk_level=finalization_metadata.get("risk_level"),
                decision_policy=decision_policy_context,
                unknowns=list(state.get("unknowns") or []),
            )
            adaptive_context.setdefault("market", {})
            adaptive_context.setdefault("history", [])
            adaptive_context["evidence_selection"] = finalized_context["evidence_selection"]
            adaptive_context["unknowns"] = finalized_context["unknowns"]
            adaptive_playstyle = adaptive_context.get("playstyle")
            if not isinstance(adaptive_playstyle, dict):
                adaptive_playstyle = {}
            finalized_playstyle = finalized_context["playstyle"]
            adaptive_playstyle["profile"] = dict(finalized_playstyle.get("profile") or {})
            adaptive_playstyle["fit"] = dict(finalized_playstyle.get("fit") or {})
            adaptive_playstyle["risk_level"] = finalized_playstyle.get("risk_level")
            adaptive_context["playstyle"] = adaptive_playstyle
        if structured_verdict:
            authoritative_context = {
                "kind": kind,
                "confirmed_evidence": confirmed_evidence,
                "review_outcome": state.get("review_outcome"),
                "decision_policy": _decision_policy_report_summary(decision_policy_context),
                "stock_profile": finalization_metadata.get("stock_profile") or {},
                "calibration": finalization_metadata.get("calibration") or {},
                "candidate": _verdict_report_candidate(candidate),
                "adaptive_report": adaptive_context,
            }
        else:
            authoritative_context = {
                "kind": kind,
                "confirmed_evidence": confirmed_evidence,
                "research_thesis": controller.thesis,
                "gaps": list(state.get("gaps") or []),
                "unknowns": list(state.get("unknowns") or []),
                "review_outcome": state.get("review_outcome"),
                "review_issues": list(state.get("review_issues") or []),
                "decision_policy": report_decision_policy,
                "stock_profile": finalization_metadata.get("stock_profile") or {},
                "calibration": finalization_metadata.get("calibration") or {},
            }
        if kind == "position_action":
            authoritative_context.update({
                "effective": candidate,
                "baseline_for_change_explanation": state.get("position_baseline") or {},
            })
        elif not structured_verdict:
            authoritative_context["candidate"] = candidate
            authoritative_context["adaptive_report"] = adaptive_context
            if not decision_policy_context and not authoritative_report_context:
                authoritative_context["system_context"] = system_context
        report_contract = (
            "你是 Apex 的正式投资分析报告编辑。候选结论已经完成证据门控和独立复核，"
            "你只能解释它，不得改变其中任何机器字段。基于给定对话、工具结果、证据和候选结论，"
            "输出唯一一份干净的 Markdown 正式报告；不要调用工具，不要描述查询过程或自我修正。\n\n"
            + format_contract
            + "基本面分析必须引用具体营收、利润、现金流、ROE、负债或估值数据；数据未知就明确写未知，禁止编造。"
            "多头和空头各写 0-3 条经后端计入的证据，不得为了凑数添加论据；"
            + (
                "裁判结论中的证据覆盖率由系统写入；不要自行输出或计算证据覆盖率。"
                "裁判结论必须逐字写出 `**净硬度：<net_hardness>**`。"
                if kind == "verdict" else
                "裁判必须引用权威上下文 decision_policy 中的覆盖率、净硬度和去重结果。"
            )
            + "置信度只解释后端的一次性校准结果，不得再次手工加减。"
            "操作建议必须与最终方向及价格建议一致。\n\n"
            "以下是正式报告的权威上下文。confirmed_evidence 视为已验证；"
            "原始对话中的未收录材料不得覆盖它。"
            + (
                "effective 是不可修改的最终机器结果；baseline_for_change_explanation 只可解释变化，绝不可作为另一份结果。\n"
                if kind == "position_action" else "candidate 是不可修改的最终机器结果：\n"
            )
            + json.dumps(authoritative_context, ensure_ascii=False, sort_keys=True)
        )
        issues: list[str] = []
        terminal_provider_failure = False
        for attempt in range(2):
            retry = ""
            if issues:
                retry = (
                    "\n\n上一版未通过确定性质量门。必须逐条修正，且不要解释修正过程：\n- "
                    + "\n- ".join(issues)
                )
                emit({"type": "report_retry", "attempt": attempt + 1, "issues": issues})
            try:
                prior_messages = (
                    []
                    if decision_policy_context or (kind == "verdict" and authoritative_report_context)
                    else list(state.get("messages") or [])
                )
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        *prior_messages,
                        {"role": "user", "content": report_contract + retry},
                    ],
                    max_tokens=16384, temperature=0.2 if attempt == 0 else 0,
                    extra_body={"thinking": {"type": "disabled"}},
                )
            except Exception as exc:
                terminal_provider_failure = True
                issues = [f"正式报告生成失败：{exc}"]
                continue
            terminal_provider_failure = False
            _record_usage(response, call="report", iteration=int(state.get("model_iterations", 0)))
            choice = response.choices[0]
            text = str(choice.message.content or "").strip()
            if kind == "verdict":
                text = _normalize_report_evidence_coverage(text, candidate)
            if getattr(choice, "finish_reason", None) == "length":
                issues = ["正式报告输出被 max_tokens 截断"]
            else:
                if structured_verdict:
                    issues = _validate_final_report(
                        text, kind, candidate, adaptive_report=adaptive_context,
                    )
                else:
                    issues = _validate_final_report(text, kind, candidate)
            if not issues:
                emit({"type": "report_generated", "attempt": attempt + 1})
                return {
                    "analysis_text": text, "report_route": "finalize",
                    "report_validation_issues": [], "report_generation_attempts": attempt + 1,
                    "draft_data": candidate,
                    "finalization_metadata": finalization_metadata,
                }

        failures = [f"report: {issue}" for issue in issues]
        controller.failures.extend(
            failure for failure in failures if failure not in controller.failures
        )
        provider_failed = terminal_provider_failure
        return {
            "analysis_text": "", "report_route": "abstain",
            "report_validation_issues": issues, "report_generation_attempts": 2,
            "outcome_reason": "provider_failure" if provider_failed else "report_validation_failed",
            "next_actions": [
                "模型服务恢复后重新运行分析"
                if provider_failed else "重新运行分析并生成完整正式报告"
            ],
            "failures": list(controller.failures),
            "research_metrics": controller.research_metrics(
                stop_reason="provider_failure" if provider_failed else "report_validation_failed"
            ),
        }

    def finalize(state):
        kind = state.get("draft_kind")
        data_value = state.get("draft_data") or {}
        emit({
            "type": "verdict_recorded" if kind == "verdict" else "position_action_recorded",
            "iteration": state.get("model_iterations", 0),
            "verdict": data_value.get("verdict"), "confidence": data_value.get("confidence"),
            "action": data_value.get("action"),
        })
        emit({"type": "status", "stage": "completed", "message": "分析与独立复核已完成"})
        return {"analysis_status": "completed", "token_usage": dict(usage_stats)}

    def abstain(state):
        review_issues = list(state.get("review_issues") or controller.review_issues)
        outcome_reason = str(state.get("outcome_reason") or "evidence_gap")
        unknowns = list(state.get("unknowns") or [])
        if outcome_reason == "evidence_gap" and not unknowns:
            unknowns = [
                item for item in controller.finalization_decision().blockers
                if item not in {"Agent 尚未声明证据收敛", "复核返工尚未取得新增证据并重新评估"}
            ]
        if review_issues and any(issue.startswith("独立复核失败:") for issue in review_issues):
            controller.failures.extend(issue for issue in review_issues if issue not in controller.failures)
        emit({"type": "status", "stage": "abstained", "message": "分析未形成可执行结论"})
        emit({"type": "analysis_abstained", "unknowns": unknowns, "outcome_reason": outcome_reason})
        return {
            "analysis_status": "insufficient_evidence", "unknowns": unknowns,
            "attempted_tools": list(controller.attempted_tools),
            "failures": list(controller.failures),
            "evidence": list(controller.evidence.values()),
            "token_usage": dict(usage_stats),
            "outcome_reason": outcome_reason,
            "next_actions": list(state.get("next_actions") or controller.next_actions),
            "research_metrics": dict(state.get("research_metrics") or controller.research_metrics()),
        }

    graph = build_analysis_graph(GraphHandlers(
        prepare=prepare, safety_scan=safety_scan, reason=reason,
        execute_tools=execute_tools, assess=assess, draft=draft,
        review=review, report=report, finalize=finalize, abstain=abstain,
    ))
    return graph.invoke({}, {
        "recursion_limit": max(30, max_iter * 5),
        "run_name": "apex-analysis-graph",
        "tags": ["apex", "stock-analysis", ts_code],
        "metadata": {"ts_code": ts_code, "model": model},
    })


def _run_analysis(ts_code: str, save: bool = True, on_progress=None,
                  candidate_context: Optional[dict] = None) -> dict:
    """
    Run full agent analysis for ts_code via DeepSeek API.
    on_progress(event: dict) is called for each milestone (context injection /
    AI assistant text / tool call / tool result / verdict). See apex/trace.py
    for event schema. Returns the parsed verdict dict. Saves to journal if save=True.
    """
    cfg = _cfg_mod.get()
    model = cfg["deepseek"]["model"]
    max_iter = cfg["deepseek"]["max_tool_iterations"]
    history_limit = cfg["deepseek"].get("history_limit", 8)
    client = _make_client(cfg)
    candidate_context = dict(candidate_context or {"source_type": "manual", "red_flag": False})
    candidate_context.setdefault("source_type", "manual")
    candidate_context.setdefault("red_flag", False)
    candidate_block = "## 候选来源与交易上下文\n" + json.dumps(
        candidate_context, ensure_ascii=False, default=str,
    )

    events: list[dict] = []

    def _emit(event: dict) -> None:
        events.append(event)
        if on_progress:
            try:
                on_progress(event)
            except Exception:
                pass

    history_entries = journal.load_verdicts(ts_code=ts_code)
    forecast_rows = forecast_calibration.refresh_forecast_rows(journal.load_verdicts())
    history_block = _format_history(
        history_entries, ts_code=ts_code, limit=history_limit,
    )
    try:
        from apex import watchlist as _wl_run
        position_baseline = deepcopy(next(
            (position for position in _wl_run.load().get("active_positions", [])
             if position.get("ts_code") == ts_code),
            {},
        ))
    except Exception:
        position_baseline = {}
    portfolio_block = _format_portfolio_context(ts_code, position_baseline)
    intraday_block, intraday_ctx = _format_intraday_block(ts_code)
    market_block, market_ctx = _format_market_context(ts_code)
    # 把盘中走势快照并入 market_context，事后复盘一处看全
    if intraday_ctx:
        market_ctx["intraday"] = intraday_ctx

    # ── Playstyle Engine v1（skill 分支，D9 <70% 后启用）──
    # FE 特征注入 prompt 供 AI 判定玩法（record_verdict 填 playstyle）；playstyle 由 AI 填，
    # finalize_playstyle 规整 + 软门控（gate#1 auto-fix primary / gate#2 flag，OV#6 不 reject）。
    # FE<0.5 -> block 标降级，AI 可不填 playstyle（entry=null）。try/except 防数据源失败卡死 loop。
    playstyle_block = ""
    playstyle_feats: dict = {}
    try:
        playstyle_block, playstyle_feats = _format_playstyle_block(ts_code)
    except Exception as e:
        playstyle_block = f"## 玩法特征\n（加载失败: {type(e).__name__}: {e}）"
        playstyle_feats = {"completeness": 0.0, "risk_level": None, "features": {}, "notes": [f"FE 异常: {e}"]}

    report_context = build_report_context(
        history_entries=history_entries,
        market_context=market_ctx,
        playstyle=None,
        playstyle_features=playstyle_feats,
        playstyle_fit=None,
        risk_level=playstyle_feats.get("risk_level"),
        decision_policy={},
        unknowns=[],
    )

    stock_profile = _stock_profile_from_context(ts_code, playstyle_feats, candidate_context)
    initial_evidence = _system_evidence_items(
        ts_code=ts_code, profile=stock_profile, playstyle_feats=playstyle_feats,
        market_ctx=market_ctx, intraday_ctx=intraday_ctx,
    )
    policy_block = _format_policy_context(stock_profile, initial_evidence)
    selected_skills = ["playstyle"]
    if stock_profile.growth_valuation_mode != "disabled":
        selected_skills.append("growth-stock")
    system = _load_system_prompt(skill_names=selected_skills)

    _emit({"type": "context", "name": "history", "content": history_block})
    _emit({"type": "context", "name": "portfolio", "content": portfolio_block})
    _emit({"type": "context", "name": "intraday", "content": intraday_block})
    _emit({"type": "context", "name": "market", "content": market_block})
    _emit({"type": "context", "name": "playstyle", "content": playstyle_block})
    _emit({"type": "context", "name": "decision_policy", "content": policy_block})
    _emit({"type": "context", "name": "candidate", "content": candidate_block})

    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"请分析股票 {ts_code}。\n\n"
                f"## 该标的过往判断（最近 {history_limit} 次，含实际后续表现）\n{history_block}\n\n"
                f"{portfolio_block}\n\n"
                f"{intraday_block}\n\n"
                f"{market_block}\n\n"
                f"{playstyle_block}\n\n"
                f"{policy_block}\n\n"
                f"{candidate_block}\n\n"
                "工作流：先分析已注入的结构化上下文，再按关键缺口调用工具；每轮补证后调用 "
                "submit_research_state。证据收敛后提交 record_verdict。\n"
                "record_verdict 中每边提交 0-3 条 evidence_claims，必须引用系统或工具返回的 "
                "evidence_id；不得凑数，不得把同源市场情绪拆成多条。\n"
                "只填写 model_confidence，不要应用历史胜率、市场环境或未反驳论点等人工加减分；"
                "股票类型、四维权重、最终方向和最终置信度由后端确定。\n"
                "盘中数据 is_complete=false，只能作风险提示，不能确认日线突破或反转。"
            ),
        },
    ]

    def _prepare_policy_candidate(kind: str, candidate: dict, ledger: list[dict]) -> tuple[dict, dict]:
        if kind != "verdict":
            return dict(candidate), {}
        policy_candidate, metadata = apply_accuracy_policy(
            {
                **candidate,
                "stock_type": stock_profile.primary_type,
                "growth_valuation_mode": stock_profile.growth_valuation_mode,
            },
            stock_profile,
            {
                "analysis_date": datetime.now(_TZ_CN).date().isoformat(),
                "evidence_ledger": {item["id"]: item for item in ledger},
                "sector_available": bool(market_ctx.get("sector")),
            },
            forecast_rows=forecast_rows,
        )
        limited, repeat_metadata = _audit_repeat_candidate(
            policy_candidate, history_entries,
        )
        metadata.update(repeat_metadata)
        metadata["stock_profile"] = stock_profile.to_dict()
        return limited, metadata

    def _finalize_report_candidate(kind: str, candidate: dict) -> tuple[dict, dict]:
        if kind != "verdict":
            return dict(candidate), {}
        report_playstyle = playstyle.finalize_playstyle(
            candidate.get("playstyle"), playstyle_feats,
        )
        return dict(candidate), {
            "playstyle": report_playstyle,
            "playstyle_fit": playstyle.compute_playstyle_fit(report_playstyle),
            "risk_level": playstyle_feats.get("risk_level"),
        }

    graph_result = _run_langgraph_loop(
        ts_code=ts_code, client=client, model=model, messages=messages,
        max_iter=max_iter, emit=_emit,
        system_context="\n\n".join(
            block for block in (
                history_block, portfolio_block, intraday_block, market_block, playstyle_block,
                policy_block, candidate_block,
            ) if block
        ),
        report_context=report_context,
        finalize_candidate=_finalize_report_candidate,
        prepare_candidate=_prepare_policy_candidate,
        initial_evidence=initial_evidence,
        position_baseline=position_baseline,
    )
    analysis_text = str(graph_result.get("analysis_text") or "")
    evidence_items = list(graph_result.get("evidence") or [])
    token_usage = graph_result.get("token_usage") or {}

    if graph_result.get("analysis_status") == "insufficient_evidence":
        from apex.evidence_control import build_insufficient_entry
        now_cn = datetime.now(_TZ_CN)
        outcome_reason = str(graph_result.get("outcome_reason") or "evidence_gap")
        summaries = {
            "evidence_gap": "已完成可用数据核验，但关键事实仍未得到可靠证据支持，暂不判断。",
            "research_budget_exhausted": "本轮研究未在预算内完成，请按建议动作补充信息或重新运行。",
            "model_iteration_exhausted": "证据已完成评估，但模型未能生成有效的结构化结论。",
            "provider_failure": "模型或数据服务暂不可用，本次未形成投资判断。",
            "review_failure": "候选结论未能完成独立复核，本次不输出投资判断。",
            "report_validation_failed": "候选结论已通过复核，但正式报告未通过完整性或一致性校验。",
        }
        entry = build_insufficient_entry(
            ts_code=ts_code, name=data.get_name_map().get(ts_code),
            unknowns=list(graph_result.get("unknowns") or []),
            evidence=evidence_items,
            attempted_tools=list(graph_result.get("attempted_tools") or []),
            failures=list(graph_result.get("failures") or []),
            research_summary=summaries.get(outcome_reason, summaries["evidence_gap"]),
            analyzed_at=now_cn.isoformat(timespec="seconds"),
            analysis_text=analysis_text,
            outcome_reason=outcome_reason,
            next_actions=list(graph_result.get("next_actions") or []),
            research_metrics=dict(graph_result.get("research_metrics") or {}),
        )
        entry["market_context"] = market_ctx
        entry["candidate_context"] = candidate_context
        entry["prompt_version"] = "3.1.0-decision-policy"
        entry["policy_version"] = "decision-policy-v1"
        entry["stock_profile"] = stock_profile.to_dict()
        entry["decision_policy"] = None
        entry["model_verdict"] = None
        entry["model_confidence"] = None
        entry["forecast_outcome"] = None
        entry["data_quality"] = {
            "sector_available": bool(market_ctx.get("sector")),
            "excluded_claims": [],
            "counted_evidence_ids": [],
        }
        entry["token_usage"] = token_usage
        if save:
            journal.write_entry(entry)
            try:
                trace_mod.write_trace(ts_code, entry["analyzed_at"], events)
            except Exception as exc:
                print(f"⚠ trace 写入失败（不影响 journal）: {exc}")
        return entry

    draft_kind = graph_result.get("draft_kind")
    draft_data = dict(graph_result.get("draft_data") or {})
    draft_proposal = dict(graph_result.get("draft_proposal") or {})
    verdict_data = draft_data if draft_kind == "verdict" else {}
    position_action_proposal = draft_proposal if draft_kind == "position_action" else {}
    position_action_effective = draft_data if draft_kind == "position_action" else {}
    searches_performed: list[str] = []

    # ── v1.1.0 双路径分支：持仓路径直接收尾，不走 calibration/24h 限幅 ──
    if draft_kind == "position_action" and position_action_proposal and position_action_effective:
        return _finalize_position_action(
            ts_code, position_action_proposal, position_action_effective, analysis_text, events,
            playstyle_feats, market_ctx, save, evidence_items=evidence_items,
            token_usage=token_usage, position_baseline=position_baseline,
        )

    limited_verdict = verdict_data["verdict"]
    limited_confidence = verdict_data.get("confidence")
    repeat_info = dict(
        (graph_result.get("finalization_metadata") or {}).get("repeat_analysis") or {}
    )

    cal_score = verdict_data.get("calibrated_confidence", limited_confidence)
    cal_explanation = str(verdict_data.get("calibration_explanation") or "")

    now_cn = datetime.now(_TZ_CN)
    # 中文名: 优先 name map(一次拉全量), 兜底 None。写入 journal 供历史列表直接展示。
    stock_name = data.get_name_map().get(ts_code)

    # playstyle: AI 经 record_verdict 填 -> finalize 规整 + 软门控（gate#1 auto-fix / gate#2 flag）。
    # FE<0.5 -> None；AI 未填但 FE>=0.5 -> Python fallback。compute_playstyle_fit v1 恒 insufficient_data。
    try:
        playstyle_value = playstyle.finalize_playstyle(verdict_data.get("playstyle"), playstyle_feats)
    except Exception:
        playstyle_value = None
    playstyle_fit = playstyle.compute_playstyle_fit(playstyle_value)

    entry = {
        "ts_code": ts_code,
        "name": stock_name,
        "date": now_cn.date().isoformat(),
        "analyzed_at": now_cn.isoformat(timespec="seconds"),
        "analysis_status": "completed",
        "verdict": limited_verdict,
        "opinion_verdict": limited_verdict,
        "proposed_trade_action": verdict_data.get("proposed_trade_action") or "watch",
        "confidence": limited_confidence,
        "calibrated_confidence": cal_score,
        "calibration_explanation": cal_explanation,
        "price_advice": {
            "entry": verdict_data.get("entry"),
            "entry_low": verdict_data.get("entry_low"),
            "entry_high": verdict_data.get("entry_high"),
            "stop_loss": verdict_data.get("stop_loss"),
            "target": verdict_data.get("target"),
            "position_size_pct": verdict_data.get("position_size_pct"),
            "entry_style": verdict_data.get("entry_style"),
            "valid_for_days": verdict_data.get("valid_for_days"),
        },
        "features": verdict_data.get("features", {}),
        "evidence": evidence_items,
        "evidence_text": verdict_data.get("evidence", []),
        "unknowns": [],
        "research_summary": "结构化数据与按需补证已收敛，并通过独立复核。",
        "searches_performed": searches_performed,
        "market_context": market_ctx,
        "analysis_text": analysis_text.strip(),
        "repeat_analysis": repeat_info,
        "prompt_version": "3.1.0-decision-policy",
        "source": candidate_context.get("strategy") or candidate_context.get("source_type") or "manual",
        "candidate_context": candidate_context,
        "decision_schema_version": "1.0",
        "token_usage": token_usage,
        "setup_tag": verdict_data.get("setup_tag"),
        "stock_type": verdict_data.get("stock_type"),
        "stock_profile": stock_profile.to_dict(),
        "decision_policy": dict(
            (graph_result.get("finalization_metadata") or {}).get("decision_policy") or {}
        ),
        "model_verdict": verdict_data.get("model_verdict"),
        "model_confidence": verdict_data.get("model_confidence"),
        "forecast_outcome": None,
        "policy_version": "decision-policy-v1",
        "data_quality": {
            "sector_available": bool(market_ctx.get("sector")),
            "excluded_claims": list((
                (graph_result.get("finalization_metadata") or {}).get("decision_policy") or {}
            ).get("excluded_claims") or []),
            "counted_evidence_ids": [
                item.get("evidence_id") for item in (
                    ((graph_result.get("finalization_metadata") or {}).get("decision_policy") or {})
                    .get("counted_claims") or []
                )
            ],
        },
        "valuation_basis": verdict_data.get("valuation_basis"),
        # ── Playstyle Engine v1（A2 持久化 FE 特征 / D13 risk_level / skill 分支 AI 填 playstyle / T6 契合度脚手架）──
        "playstyle": playstyle_value,                       # {ratings,primary,secondary,reasons,method,low_confidence} 或 null(FE<0.5)
        "playstyle_fit": playstyle_fit,                     # v1 恒 insufficient_data（D10，规则 v1.1）
        "playstyle_features": playstyle_feats,              # FE 10 特征 + completeness + risk_level（A2 可复现/可解释）
        "risk_level": playstyle_feats.get("risk_level"),    # low/medium/high，与 playstyle 正交（D13）
    }
    entry["trade_decision"] = trade_signal.evaluate_trade_proposal(entry)

    if save:
        journal.write_entry(entry)
        try:
            trace_mod.write_trace(ts_code, entry["analyzed_at"], events)
        except Exception as e:
            print(f"⚠ trace 写入失败（不影响 journal）: {e}")
        print(f"✓ 已保存到日志: {ts_code} → {entry['verdict']} (置信度 {entry['confidence']})")

    # 推送分析完成通知（仅看多/偏多，避免中性/偏空刷屏）
    if entry["verdict"] in BULLISH_VERDICTS:
        try:
            from apex import notify as _notify
            _pa = entry.get("price_advice") or {}
            _parts = [f"verdict {entry['verdict']} (置信度 {entry.get('confidence')})"]
            if _pa.get("entry"):
                _parts.append(f"入场 {_pa['entry']}")
            if _pa.get("stop_loss"):
                _parts.append(f"止损 {_pa['stop_loss']}")
            if _pa.get("target"):
                _parts.append(f"目标 {_pa['target']}")
            _notify.notify(
                f"📊 分析完成 {entry.get('name') or ''} {ts_code}".strip(),
                " ｜ ".join(_parts),
            )
        except Exception:
            pass  # 推送失败不阻断分析

    entry["_trace_events"] = events  # 当前会话直接用，不序列化到 journal
    return entry


def run(ts_code: str, save: bool = True, on_progress=None,
        candidate_context: Optional[dict] = None) -> dict:
    """Run one stock analysis with optional, observer-safe LangSmith tracing."""
    cfg = _cfg_mod.get()
    normalized_context = dict(candidate_context or {"source_type": "manual", "red_flag": False})
    normalized_context.setdefault("source_type", "manual")
    normalized_context.setdefault("red_flag", False)
    try:
        from apex import watchlist as _wl_trace
        held = any(
            position.get("ts_code") == ts_code
            for position in _wl_trace.load().get("active_positions", [])
        )
    except Exception:
        held = False

    with observability.analysis_trace(
        ts_code=ts_code,
        save=save,
        candidate_context=normalized_context,
        model=cfg["deepseek"]["model"],
        prompt_version="3.1.0-decision-policy",
        held=held,
    ) as span:
        result = _run_analysis(
            ts_code,
            save=save,
            on_progress=on_progress,
            candidate_context=normalized_context,
        )
        span.set_outputs({
            "analysis_status": result.get("analysis_status"),
            "outcome_reason": result.get("outcome_reason"),
            "verdict": result.get("verdict"),
            "model_verdict": result.get("model_verdict"),
            "model_confidence": result.get("model_confidence"),
            "calibrated_confidence": result.get("calibrated_confidence"),
            "stock_profile": result.get("stock_profile"),
            "decision_policy": result.get("decision_policy"),
            "policy_version": result.get("policy_version"),
            "action": (result.get("position_action") or {}).get("action"),
            "token_usage": result.get("token_usage") or {},
        })
        return result


def _simulate_ladder(
    levels: list, current_price, eff_stop, eff_target
) -> tuple[list[str], list[str]]:
    """ladder 路径模拟校验：ladder 的执行语义是按价格顺序触发，校验照执行方式把 ladder 走一遍。

    下行路径（现价往下，trigger 降序）必须单一意图（纯 add 或纯 trim）：
    先卖后买/先买后卖 = churn（拒）。上行路径允许 add->trim（金字塔加仓后高位兑现），
    trim->add = 卖了更高价接回（拒）。trim 低于路径上的有效止损 = 死档（拒；初始止损或
    路径内 add 上移后的新止损）。上行档 new_stop 杀死下行 trim = 条件死档（advisory 不拒，
    仅当上行执行到时才失效）。add ≥ 有效止盈 = 自相矛盾（拒）。

    current_price=None -> 降级为锚定无关检查（不做路径顺序/单一意图检查）。
    返回 (rejects, advisories)，调用方分别并入拒绝理由与 advisory 警告。
    """
    parsed: list[tuple[int, str, float, float | None]] = []  # (原档序, action, trigger, new_stop)
    for _i, _lvl in enumerate(levels or []):
        if not isinstance(_lvl, dict):
            continue
        _act = _lvl.get("action")
        if _act not in ("add", "trim"):
            continue
        try:
            _tp = float(_lvl.get("trigger_price"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(_tp):
            continue
        try:
            _nsf = float(_lvl.get("new_stop"))
        except (TypeError, ValueError, OverflowError):
            _nsf = None
        if _nsf is not None and not math.isfinite(_nsf):
            _nsf = None
        parsed.append((_i, _act, _tp, _nsf))

    rejects: list[str] = []
    advisories: list[str] = []

    try:
        _cp = float(current_price) if current_price is not None else None
    except (TypeError, ValueError):
        _cp = None

    if _cp is None:
        # 降级（锚定无关）：trim < 有效止损 -> 拒；add ≥ 有效止盈 -> 拒；
        # add 档 new_stop 杀死 trim -> advisory（仅 add 杀手，同旧守卫 6）。
        if eff_stop is not None:
            for _i, _act, _tp, _nsf in parsed:
                if _act == "trim" and _tp < eff_stop:
                    rejects.append(
                        f"scale_plan 第 {_i + 1} 档 trim 触发价 {_tp} 低于有效止损 {eff_stop}："
                        f"止损会先触发全仓离场，该减仓档永远不会执行（死代码）。"
                        f"请把该档 trigger_price 提到 {eff_stop} 之上，或把 new_stop 降到该触发价之下。"
                    )
        if eff_target is not None:
            for _i, _act, _tp, _nsf in parsed:
                if _act == "add" and _tp >= eff_target:
                    rejects.append(
                        f"scale_plan 第 {_i + 1} 档 add 触发价 {_tp} ≥ 有效止盈价 {eff_target}："
                        f"到止盈价会触发止盈提醒/离场，又在同等或更高价加仓，计划自相矛盾。"
                        f"请用 new_target 上移止盈（当前 {eff_target}），或移除/下修该加仓档。"
                    )
        _add_stops = [(_i, _nsf) for _i, _act, _tp, _nsf in parsed if _act == "add" and _nsf is not None]
        for _ti, _act, _ttp, _ in parsed:
            if _act != "trim":
                continue
            _killers = [(_s, _ai) for _ai, _s in _add_stops if _ttp < _s]
            if _killers:
                _s, _ai = min(_killers)
                advisories.append(
                    f"第 {_ti + 1} 档 trim@{_ttp:g} 在第 {_ai + 1} 档 add 执行后"
                    f"（止损上移至 {_s:g}）永不触发"
                )
        return rejects, advisories

    _down = sorted((p for p in parsed if p[2] < _cp), key=lambda p: -p[2])   # 现价往下，降序触发
    _up = sorted((p for p in parsed if p[2] >= _cp), key=lambda p: p[2])     # 现价往上，升序触发

    # 下行路径：单一意图（纯 add 或纯 trim），止损随档上移，低于有效止损的 trim 是死档
    _stop_w = eff_stop
    _prev = None  # (idx, action) 上一触发档
    for _i, _act, _tp, _nsf in _down:
        if _prev is not None and _act != _prev[1]:
            if _prev[1] == "trim":
                rejects.append(
                    f"下行路径：第 {_prev[0] + 1} 档 trim 先于第 {_i + 1} 档 add@{_tp:g} 触发——"
                    f"同一下行路径先卖后买是 churn（两档应互斥：破位减 vs 企稳加）。"
                    f"请合并为单一防守档，或把 add 触发价拉开到不同情景。"
                )
            else:
                rejects.append(
                    f"下行路径：第 {_prev[0] + 1} 档 add 先于第 {_i + 1} 档 trim@{_tp:g} 触发——"
                    f"同一下行路径先买后卖是 churn。请合并为单一意图（纯回踩加仓或纯防守减仓）。"
                )
        if _act == "trim" and _stop_w is not None and _tp < _stop_w:
            rejects.append(
                f"下行路径：第 {_i + 1} 档 trim@{_tp:g} 低于有效止损 {_stop_w:g}，"
                f"止损先触发全仓离场，该减仓档永不触发（死档）。"
                f"请把 trigger_price 提到 {_stop_w:g} 之上，或下修止损。"
            )
        _prev = (_i, _act)
        if _nsf is not None:
            _stop_w = _nsf if _stop_w is None else max(_stop_w, _nsf)

    # 上行路径：先加后减（金字塔），trim 之后不得再有 add；add 不得 ≥ 有效止盈；记录止损上移
    _prev = None
    _up_stops: list[tuple[int, str, float]] = []  # (idx, action, new_stop) 供跨路径 advisory
    for _i, _act, _tp, _nsf in _up:
        if _act == "add":
            if _prev is not None and _prev[1] == "trim":
                rejects.append(
                    f"上行路径：第 {_prev[0] + 1} 档 trim 之后第 {_i + 1} 档 add@{_tp:g} 更高价接回（churn）。"
                    f"上行路径应为先加后减（金字塔），trim 之后不允许再有 add 档。"
                )
            if eff_target is not None and _tp >= eff_target:
                rejects.append(
                    f"上行路径：第 {_i + 1} 档 add@{_tp:g} ≥ 有效止盈价 {eff_target}："
                    f"到止盈价会触发止盈提醒/离场，又在同等或更高价加仓，计划自相矛盾。"
                    f"请用 new_target 上移止盈（当前 {eff_target}），或移除/下修该加仓档。"
                )
        _prev = (_i, _act)
        if _nsf is not None:
            _up_stops.append((_i, _act, _nsf))

    # 跨路径条件死档（advisory）：上行任一档 new_stop 上移止损后，低于新止损的下行 trim 永不触发
    for _ti, _act, _ttp, _ in _down:
        if _act != "trim":
            continue
        _killers = [(_s, _ki, _ka) for _ki, _ka, _s in _up_stops if _ttp < _s]
        if _killers:
            _s, _ki, _ka = min(_killers)
            advisories.append(
                f"第 {_ti + 1} 档 trim@{_ttp:g} 在第 {_ki + 1} 档 {_ka} 执行后"
                f"（止损上移至 {_s:g}）永不触发"
            )

    return rejects, advisories


def _prepare_position_action_candidate(
    tool_input: dict, baseline: dict, ts_code: str, current_price,
) -> tuple[dict, dict, list[str]]:
    """Adapt, materialize, and business-validate one position-action proposal."""
    proposal = adapt_legacy_proposal(tool_input)
    shape_issues = validate_proposal_shape(proposal)
    if shape_issues:
        return proposal, {}, shape_issues
    effective = materialize_effective_position_plan(baseline, proposal)
    validation_input = {
        **proposal,
        "new_stop": effective["effective_stop"],
        "new_target": effective["effective_target"],
        "scale_plan": effective["effective_scale_plan"],
    }
    reasons, _ = _validate_position_action(
        validation_input, ts_code, [], current_price=current_price,
    )
    return proposal, effective, reasons


def _validate_position_action(
    tool_input: dict, ts_code: str, searches_performed: list, current_price=None
) -> tuple[list[str], str]:
    """v1.1.0 record_position_action dispatch 守卫：对称守卫 + 字段校验 + trim/exit 强制搜索 + 4h 反 churn + ladder 路径模拟 + sizing_cap warn。

    current_price 供 ladder 路径模拟锚定（dispatch 处取实时价/日线收盘，None -> 降级为锚定无关检查）。
    返回 (reject_reasons, sizing_warn)。reasons 非空 -> 拒绝回喂 AI；sizing_warn 非空 -> advisory 警告仍接受。
    抽离自 agent loop 供单测（T6）。
    """
    from apex import watchlist as _wl_pa
    try:
        _held_pa = next(
            (p for p in _wl_pa.load().get("active_positions", [])
             if p.get("ts_code") == ts_code), None
        )
    except Exception:
        _held_pa = None
    action = tool_input.get("action")
    reasons: list[str] = []

    # 守卫 1: 持仓必须存在（防 _format_portfolio_context 快照后平仓竞态）
    if _held_pa is None:
        reasons.append(f"{ts_code} 已不持仓（可能在分析期间平仓）。未持仓票请调 record_verdict。")

    # 守卫 2: 字段校验（add->add_shares>0; trim->恰好一个 trim_shares/trim_pct）
    if action == "add":
        _asz = tool_input.get("add_shares")
        if not (isinstance(_asz, int) and _asz > 0):
            reasons.append("action=add 必填 add_shares 且为 > 0 的整数。")
    elif action == "trim":
        _ts = tool_input.get("trim_shares")
        _tp = tool_input.get("trim_pct")
        _has_ts = isinstance(_ts, int) and _ts > 0
        _has_tp = isinstance(_tp, (int, float)) and 0 < float(_tp) <= 1
        if _has_ts == _has_tp:
            reasons.append("action=trim 必须恰好填一个：trim_shares(>0 整数) 或 trim_pct(0-1]。")
    elif action not in ("hold", "exit"):
        reasons.append(f"action 必须是 hold/add/trim/exit，got {action!r}。")

    # 守卫 3 (OV#7): 4h 反 churn -- 距上次 position_action <4h 且无 new_info -> 拒
    if _held_pa is not None and not reasons:
        try:
            _pas = journal.load_position_actions(ts_code)
            if _pas:
                _last_pa = sorted(
                    _pas, key=lambda e: e.get("analyzed_at") or e.get("date", "")
                )[-1]
                _last_at = _last_pa.get("analyzed_at") or _last_pa.get("date")
                if _last_at:
                    try:
                        _last_dt = datetime.fromisoformat(_last_at)
                        if _last_dt.tzinfo is None:
                            _last_dt = _last_dt.replace(tzinfo=_TZ_CN)
                        _hours = (datetime.now(_TZ_CN) - _last_dt).total_seconds() / 3600.0
                        if 0 <= _hours < 4 and not tool_input.get("new_info"):
                            reasons.append(
                                f"距上次加减仓建议仅 {_hours:.1f}h（<4h），频繁更新 ladder 会 churn。"
                                "如确有新增信息（公告/放量/形态突破），请在 new_info 列出后重调。"
                            )
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    # 守卫 5-7 (ladder 自洽 -> 路径模拟): ladder 语义=按价格顺序触发，校验照执行方式走一遍
    # （_simulate_ladder）。有效止损 = new_stop（若给）否则持仓 stop_loss；有效止盈 = new_target（若给）
    # 否则持仓 target。current_price 缺失时降级为锚定无关检查（不做路径顺序/单一意图检查）。
    ladder_warn = ""
    if _held_pa is not None:
        _ns = tool_input.get("new_stop")
        _eff_stop = _ns if isinstance(_ns, (int, float)) and not isinstance(_ns, bool) else _held_pa.get("stop_loss")
        try:
            _eff_stop_f = float(_eff_stop) if _eff_stop is not None else None
        except (TypeError, ValueError):
            _eff_stop_f = None
        _nt = tool_input.get("new_target")
        _eff_tgt = _nt if isinstance(_nt, (int, float)) and not isinstance(_nt, bool) else _held_pa.get("target")
        try:
            _eff_tgt_f = float(_eff_tgt) if _eff_tgt is not None else None
        except (TypeError, ValueError):
            _eff_tgt_f = None
        _rejects, _advisories = _simulate_ladder(
            tool_input.get("scale_plan") or [], current_price, _eff_stop_f, _eff_tgt_f
        )
        reasons.extend(_rejects)
        if _advisories:
            ladder_warn = (
                "⚠ ladder 跨档自洽：" + "；".join(_advisories) +
                "（死代码）。如属有意（trim 仅加仓前有效）请在该 trim 档 reason 注明失效条件，"
                "否则调整 trigger_price/new_stop。"
            )

    # A1 sizing_cap warn（advisory，不拒）：加仓后总风险超上限则警告仍接受
    sizing_warn = ""
    if action == "add" and _held_pa is not None and not reasons:
        try:
            from apex import account as _acct_pa
            _asz2 = int(tool_input.get("add_shares") or 0)
            _sim = [dict(_held_pa)]
            _sim[0]["position_size_shares"] = int(_held_pa.get("position_size_shares") or 0) + _asz2
            _risk = _acct_pa.current_total_risk(_sim, account=_acct_pa.load())
            if _risk.get("over_limit"):
                sizing_warn = (
                    f"⚠ 加仓后总风险 {_risk.get('total_risk_pct')}% 超上限 "
                    f"{_risk.get('max_total_risk_pct')}%（advisory 警告，仍接受建议）。"
                )
        except Exception:
            pass

    _warn = " ".join(w for w in (ladder_warn, sizing_warn) if w)
    return reasons, _warn


def _finalize_position_action(
    ts_code: str,
    proposal: dict,
    effective: dict,
    analysis_text: str,
    events: list,
    playstyle_feats: dict,
    market_ctx: dict,
    save: bool = True,
    *,
    position_baseline: dict | None = None,
    evidence_items: list | None = None,
    token_usage: dict | None = None,
) -> dict:
    """v1.1.0 持仓路径收尾：写 position_action journal entry + 刷新 active_positions.plan。

    跳过 calibration（position_action 无 confidence）+ 跳过 24h 限幅（OV#7 反 churn 已在 dispatch 拦）。
    playstyle 从最近 verdict 继承（股票客观属性，持仓期不应翻转），无 prior verdict 则从 FE 派生（OV#3）。
    verdict/price_advice/features/evidence/confidence 全 None（P1：不污染 calibration/backtest，load_verdicts 已过滤）。
    """
    now_cn = datetime.now(_TZ_CN)
    stock_name = data.get_name_map().get(ts_code)

    # playstyle: 优先继承最近 verdict 的判定（股票客观属性，稳定）；无 prior 则 FE 派生
    playstyle_value = None
    try:
        _prior = journal.load_latest_verdict(ts_code)
        if _prior and _prior.get("playstyle"):
            playstyle_value = _prior.get("playstyle")
        else:
            playstyle_value = playstyle.finalize_playstyle(None, playstyle_feats)
    except Exception:
        playstyle_value = None
    try:
        playstyle_fit = playstyle.compute_playstyle_fit(playstyle_value)
    except Exception:
        playstyle_fit = None

    entry = {
        "ts_code": ts_code,
        "name": stock_name,
        "date": now_cn.date().isoformat(),
        "analyzed_at": now_cn.isoformat(timespec="seconds"),
        "analysis_status": "completed",
        "source": POSITION_ACTION_SOURCE,
        "position_action": {
            "action": proposal.get("action"),
            "add_shares": proposal.get("add_shares"),
            "trim_shares": proposal.get("trim_shares"),
            "trim_pct": proposal.get("trim_pct"),
            # Keep legacy change fields as the proposal, while explicit effective
            # metadata describes the state reviewed and reported this run.
            "new_stop": proposal.get("new_stop"),
            "new_target": proposal.get("new_target"),
            "ladder_intent": proposal.get("ladder_intent"),
            "effective_stop": effective.get("effective_stop"),
            "effective_target": effective.get("effective_target"),
            "scale_plan": effective.get("effective_scale_plan") or [],
            "rationale": proposal.get("rationale"),
            "compatibility_warnings": list(proposal.get("compatibility_warnings") or []),
        },
        "analysis_text": analysis_text.strip(),
        "prompt_version": "3.0.0-langgraph",
        "market_context": market_ctx,
        "token_usage": dict(token_usage or {}),
        # P1: verdict/price_advice/features/evidence/confidence 全 None（不污染 calibration/backtest）
        "verdict": None,
        "confidence": None,
        "price_advice": None,
        "features": None,
        "evidence": list(evidence_items or []),
        "unknowns": [],
        "research_summary": "持仓建议已完成动态补证并通过独立复核。",
        # OV#3: playstyle 例外（股票属性，非方向 call 字段）
        "playstyle": playstyle_value,
        "playstyle_fit": playstyle_fit,
        "playstyle_features": playstyle_feats,
        "risk_level": playstyle_feats.get("risk_level"),
    }

    if save:
        from apex import watchlist as _wl_fin
        _new_plan = {
            "scale_plan": entry["position_action"]["scale_plan"],
            "doctrine": "single_v1",
            "updated_at": entry["analyzed_at"],
            # Single compare-and-apply below records this before changing effective stop/target.
            "last_action": entry["position_action"]["action"],
            "last_new_stop": entry["position_action"]["new_stop"],
        }
        baseline_for_save = position_baseline
        if baseline_for_save is None:
            # Compatibility for direct callers. run() always provides its frozen baseline.
            baseline_for_save = next(
                (item for item in _wl_fin.load().get("active_positions", [])
                 if item.get("ts_code") == ts_code),
                {},
            )
        _updated = _wl_fin.apply_position_action_if_unchanged(
            ts_code,
            baseline_for_save,
            plan=_new_plan,
            effective_stop=effective.get("effective_stop"),
            effective_target=effective.get("effective_target"),
        )
        if _updated is None:
            return {
                "ts_code": ts_code,
                "name": stock_name,
                "analysis_status": "insufficient_evidence",
                "outcome_reason": "position_changed_during_analysis",
                "unknowns": ["持仓在分析期间已平仓或交易计划已变更，未保存过期建议。"],
                "next_actions": ["刷新当前持仓后重新运行分析。"],
                "analysis_text": analysis_text.strip(),
                "evidence": list(evidence_items or []),
                "market_context": market_ctx,
                "token_usage": dict(token_usage or {}),
                "_trace_events": events,
            }
        journal.write_entry(entry)
        try:
            trace_mod.write_trace(ts_code, entry["analyzed_at"], events)
        except Exception as e:
            print(f"⚠ trace 写入失败（不影响 journal）: {e}")
        _n = len(_new_plan["scale_plan"])
        print(f"✓ 已保存加减仓建议: {ts_code} -> {entry['position_action']['action']} (ladder {_n} 档)")

    entry["_trace_events"] = events
    return entry
