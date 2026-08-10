"""
DeepSeek API (OpenAI-compatible) agent for stock analysis.
The AI autonomously calls data tools, then records verdict via record_verdict tool.
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from openai import OpenAI

_TZ_CN = timezone(timedelta(hours=8))

from apex import config as _cfg_mod, data, journal, calibration, evidence_attribution, trace as trace_mod, skills, playstyle
from apex.journal_views import history_digest
from apex.schemas import (
    VERDICT_ENUM, BULLISH_VERDICTS, BEARISH_VERDICTS,
    STOCK_TYPE_ENUM, VALUATION_BASIS_ENUM, PLAYSTYLE_ENUM,
    POSITION_ACTION_SOURCE,
)


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
                "用博查搜索股票相关信息。**必须分类搜索，不要混搜**。\n"
                "每次调用只查一个 category，AI 应分多次调用覆盖不同维度。\n"
                "\n"
                "**强制类别（record_verdict 前必须全部调用过，否则系统拒绝记录结论）**：\n"
                "  · earnings           — 业绩面（季报/预告/营收/净利润），oneMonth 窗口\n"
                "  · shareholders       — 股东动态（减持/增持/解禁/大宗交易），oneMonth 窗口\n"
                "  · regulatory         — 监管/合规（立案/处罚/诉讼/问询函），oneYear 窗口\n"
                "  · money_flow         — 资金面（北向/龙虎榜/主力/机构），oneWeek 窗口\n"
                "\n"
                "**可选类别（按需追加）**：\n"
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
                "**evidence 字段为必填**：每条格式「数据点 → 推论」，至少 3 条，"
                "必须引用工具返回的真实数字（如 close=12.34、RSI=67.2、减持公告日期），"
                "不得写泛泛的定性描述。\n"
                "**entry / stop_loss / target 为必填**：看多/偏多/观望偏多必须填具体价位，"
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
                        "description": "置信度 1-10",
                    },
                    "entry": {"type": "number", "description": "建议买入价（主锚点）。看多/偏多/观望偏多必须填具体数字，其他方向填 0。"},
                    "entry_low": {"type": "number", "description": "买入区间下沿（地板价）。回踩入场=下方支撑，突破入场=entry 主锚点。看多类必填，非看多方向不填。"},
                    "entry_high": {"type": "number", "description": "买入区间上沿（天花板）。回踩入场=entry 主锚点，突破入场=上方阻力。看多类必填，非看多方向不填。"},
                    "stop_loss": {"type": "number", "description": "止损价。看多/偏多/观望偏多必须填具体数字，其他方向填 0。"},
                    "target": {"type": "number", "description": "目标价。看多/偏多/观望偏多必须填具体数字，其他方向填 0。"},
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
                            "支撑结论的关键证据列表，格式：「数据点 → 推论」。"
                            "必须引用工具返回的真实数字，不得只写定性描述。最少 3 条。"
                            "示例：[\"close=12.34 上穿 MA20=11.80 → 均线支撑有效\","
                            "\"RSI(14)=67.2 接近超买区 → 短期追高风险\","
                            "\"2024-04-10 公告减持 500 万股 → 大股东信心不足，利空\"]"
                        ),
                        "items": {"type": "string"},
                        "minItems": 3,
                    },
                    "position_size_pct": {
                        "type": "integer",
                        "description": (
                            "建议仓位占账户总资金的百分比（0-50）。"
                            "映射：confidence 1-2→0%, 3→≤5%, 4→≤10%, 5→≤15%, "
                            "6→≤20%, 7→≤30%, 8→≤35%, 9→≤40%, 10→≤50%。"
                            "「偏多」及更弱的方向且 position_size_pct=0 表示只观察不买入。"
                            "「观望」及更弱的方向应填 0。"
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
                            "步骤 0 声明的标的类型（蓝筹白马/题材游资/周期股/成长股/均衡型）。必填。"
                            "成长股 + 偏空 + valuation_basis=static_pe_only（或缺填）会被系统拒绝，逼你补前瞻估值后重调。"
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
                "required": ["verdict", "confidence", "entry", "stop_loss", "target", "features", "evidence", "stock_type"],
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
                "scale_plan 给完整 ladder（演进当前 ladder，非替换：未触发 level 保留，可新增/调整 level/trigger/new_stop）。"
                "系统会按价格顺序模拟执行 ladder 并拒绝不自洽路径：下行路径（现价往下）必须单一意图——纯回踩加仓或纯防守减仓，先卖后买/先买后卖是 churn（两档若互斥请合并为单一防守档或拉开到不同情景）；上行路径先加后减（金字塔），trim 之后不得再有 add；trim 低于有效止损（含路径内止损上移后的新止损）是死档；add ≥ 有效止盈价是自相矛盾（用 new_target 上移止盈修复）。"
                "target 是建仓时的一次性字段，价格观上移时必须用 new_target 同步止盈，否则化石止盈（monitor 推送）会与 ladder 打架。"
                "rationale 是机器可读摘要，完整推理写进分析文本。"
                "action=trim/exit 涉及实质风险决策，调用前必须已调 web_search 的 regulatory+shareholders+money_flow 三类（缺则被拒）。"
                "4h 内重复调此工具会被反 churn 速率限制拒绝，除非 new_info 列出本次新增信息。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["hold", "add", "trim", "exit"], "description": "加减仓动作"},
                    "add_shares": {"type": "integer", "description": "action=add 必填，加仓股数(>0)"},
                    "trim_shares": {"type": "integer", "description": "action=trim 二选一，减仓股数(>0)"},
                    "trim_pct": {"type": "number", "description": "action=trim 二选一，减仓比例 0-1（如 0.33=减1/3）"},
                    "new_stop": {"type": "number", "description": "止损上移建议（add/hold 常带，advisory）"},
                    "new_target": {"type": "number", "description": "止盈价上移建议（hold/add 常带；价格观演进时同步 target，防化石止盈推送与 ladder 冲突）"},
                    "scale_plan": {
                        "type": "array",
                        "description": "完整 ladder 计划（演进当前 ladder，非替换）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "level": {"type": "integer", "description": "1=首加/首减, 2=二加..."},
                                "trigger_price": {"type": "number", "description": "触发价"},
                                "action": {"type": "string", "enum": ["add", "trim"]},
                                "shares": {"type": "integer", "description": "加/减仓股数"},
                                "pct": {"type": "number", "description": "减仓比例 0-1（trim 时 shares/pct 二选一）"},
                                "new_stop": {"type": "number", "description": "触发后止损上移到"},
                                "reason": {"type": "string", "description": "该档触发理由"},
                            },
                        },
                    },
                    "rationale": {"type": "string", "description": "机器可读摘要（why this action + ladder），完整推理写进分析文本"},
                    "new_info": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "本次相比上次 position_action 的新增信息（4h 内重复时据此豁免反 churn 速率限制）",
                    },
                },
                "required": ["action", "rationale"],
            },
        },
    },
]


def _load_system_prompt() -> str:
    cfg = _cfg_mod.get()
    prompt_path = Path(cfg["paths"]["prompt_file"]).expanduser()
    if prompt_path.exists():
        base = prompt_path.read_text(encoding="utf-8")
    else:
        base = (
            "你是一位资深A股投资顾问。对给定股票做技术面+基本面综合分析，"
            "给出明确的判断方向和具体价位建议。分析完成后必须调用 record_verdict 工具记录结论。"
        )
    base = evidence_attribution.inject_into(calibration.inject_into(base))
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
    # 领域 skill（成长股/...方法论）。applies_to 含 analyze 的全量拼接。
    # phase 1 只加不删，与现有 expert-persona.md / 注入条款暂存冗余，验证后再去重。
    base += skills.load_skills_for("analyze")
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
    return make_client(
        api_key=cfg["deepseek"]["api_key"],
        base_url=cfg["deepseek"].get("base_url", "https://api.deepseek.com"),
    )


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

    AI 拿到当前 ladder 才能「演进而非替换」；未触发 level 保留，可新增/调整。
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
        "（action=hold/add/trim/exit + 对应股数/比例 + new_stop + 完整 scale_plan ladder），"
        "调 `record_verdict` 会被系统拒绝。",
    ]
    if scale_plan:
        parts.append("当前 ladder（演进，非替换；未触发 level 保留，可新增/调整）：")
        for item in scale_plan:
            tag = "✓已触发" if item.get("executed") else "待触发"
            parts.append(
                f"- L{item.get('level', '?')} {item.get('action', '?')} @ {item.get('trigger_price')} "
                f"-> new_stop {item.get('new_stop')}（{tag}）{item.get('reason', '')}"
            )
    else:
        parts.append(
            "当前 ladder 为空（首次重新分析）：请在 scale_plan 给出完整加减仓计划"
            "（首加/首减触发价 + 止损上移节奏 + 减仓比例，锚定当前 stop/target）。"
        )
    return "\n".join(parts)


def _format_portfolio_context(candidate_ts_code: str) -> str:
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

        held = next((p for p in positions if p.get("ts_code") == candidate_ts_code), None)
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


def run(ts_code: str, save: bool = True, on_progress=None) -> dict:
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
    system = _load_system_prompt()

    events: list[dict] = []

    def _emit(event: dict) -> None:
        events.append(event)
        if on_progress:
            try:
                on_progress(event)
            except Exception:
                pass

    history_entries = journal.load_verdicts(ts_code=ts_code)
    history_block = _format_history(
        history_entries, ts_code=ts_code, limit=history_limit,
    )
    portfolio_block = _format_portfolio_context(ts_code)
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

    _emit({"type": "context", "name": "history", "content": history_block})
    _emit({"type": "context", "name": "portfolio", "content": portfolio_block})
    _emit({"type": "context", "name": "intraday", "content": intraday_block})
    _emit({"type": "context", "name": "market", "content": market_block})
    _emit({"type": "context", "name": "playstyle", "content": playstyle_block})

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
                "步骤：\n"
                "0) **股票类型分类（必须最先做，在深入分析任何数据前完成）**：\n"
                "   先调 get_fundamentals + get_stock_info + get_dragon_tiger_list，\n"
                "   拿到 circ_mv / PE_TTM / turnover_rate / industry / 龙虎榜上榜次数，\n"
                "   以及 quarters（最近4季财务）和 summary.flags（Python预计算的风险标记）后，\n"
                "   对照下表自行判断标的类型并**显式声明**：\n"
                "\n"
                "   | 类型 | 识别特征 | 财务特征 | 技术 | 基本 | 资金 | 情绪 |\n"
                "   |------|---------|---------|------|------|------|------|\n"
                "   | 蓝筹/白马 | circ_mv > 500亿, PE适中, 消费/金融/公用 | ROE>10%稳定, 负债率<60%, OCF为正 | 30% | 50% | 10% | 10% |\n"
                "   | 题材/游资 | circ_mv < 100亿, turnover >5%, 龙虎榜常客 | 仅排雷: 负债率, 连续亏损 | 35% | 15% | 30% | 20% |\n"
                "   | 周期股 | 钢铁/煤炭/有色/化工/建材/航运/养殖 | ROE周期性波动, 高杠杆需警惕 | 25% | 35% | 20% | 20% |\n"
                "   | 成长股 | circ_mv 100-500亿, PE偏高, 科技/医药/新能源 | 营收增速>20%, 毛利率扩张/稳定 | 30% | 40% | 15% | 15% |\n"
                "   | 均衡型 | 无法明确归类 | 多特征混合 | 25% | 25% | 25% | 25% |\n"
                "\n"
                "   声明格式：「**标的类型：XX**，四维权重：技术 X%/基本 X%/资金 X%/情绪 X%」\n"
                "   如果 circ_mv / PE / turnover_rate 数据缺失（新股或数据源故障），\n"
                "   根据 industry + 龙虎榜频次 + 上市时间做最佳推断，并在声明中注明「数据缺失，推断分类」。\n"
                "   **必须引用 summary.flags 中的 Python 预计算风险标记**，不要自己重新判断 ROE 趋势或负债率阈值。\n"
                "   分类完成后，后续所有步骤的分析深度和证据选择必须按上表权重分配精力。\n"
                "   **成长股特别提示**：成长股估值看「未来增长能否消化当前估值」，禁止仅凭静态 PE_TTM 偏高就偏空（详见步骤 3 成长股估值约束）。\n"
                "\n"
                "1) 数据：调用 get_daily_price（在步骤 0 之外补充 K 线数据）\n"
                "\n"
                "   **结构化补充工具（推荐使用，但非强制）**：\n"
                "   · get_unlock_schedule — 限售解禁日程；多头判断前建议查，短期大额解禁是关键利空\n"
                "   · mx_data_query — 妙想金融数据查询（东方财富）。**蓝筹/白马和成长股必须至少调用 1 次**，\n"
                "     查营收/净利润/ROE/毛利率/经营现金流等深度财务数据，否则基本面权重是空壳。\n"
                "     题材/游资股可选，但建议查一下排除业绩暴雷风险。\n"
                "   · mx_news_search — 妙想财经资讯搜索（东方财富），比博查更垂直精准，\n"
                "     适合搜研报/新闻/公告\n"
                "   · mx_stock_screen — 妙想智能选股，自然语言批量筛选候选标的，\n"
                "     如\"市盈率低于20且ROE大于15%的A股\"\n"
                "   以上 MX 工具是东财官方数据源，返回结构化数据，与博查 web_search 互补：\n"
                "   查财经数据/新闻用 MX，查监管/政策用 web_search。\n"
                "\n"
                "2) 博查 4 类强制（缺一类 record_verdict 被拒）：earnings / shareholders / regulatory / money_flow。\n"
                "   按需加 corporate_actions / research / industry / general。若召回为空，evidence 里明确写「该类别无召回」，**不要跳过调用**。\n"
                "   **股票类型提示**：蓝筹/白马和成长股在 earnings 类别中应额外关注营收/利润趋势的持续性；\n"
                "   题材/游资股在 money_flow 类别中应重点关注游资动向和席位分析。\n"
                "\n"
                "3) 三段式辩论（写在 message content 里）：\n"
                "   ### 一、多头论点（≥3 条，格式：数据点 → 推论）\n"
                "   引用 K 线/基本面/消息面的具体数字，禁空话。\n"
                "   **股票类型约束**：蓝筹/白马的多头论点中，至少 2 条必须来自基本面证据（mx_data_query / earnings 博查）；\n"
                "   题材/游资的多头论点中，资金面（龙虎榜/北向/主力流向）必须占至少 1 条。\n"
                "   ### 二、空头论点（≥3 条，禁「虽然 X 但是 Y」）\n"
                "   独立反方证据；至少 1 条直接反驳多头第 N 条；必须考虑估值/解禁减持/行业景气/技术背离/历史回撤。\n"
                "   **股票类型约束**：蓝筹/白马的空头论点必须包含估值分析（PE 历史分位 / 与行业均值对比）；\n"
                "   周期股的空头论点必须考虑周期位置（产品价格趋势 / 产能周期 / 库存水平）。\n"
                "   **成长股估值约束（禁止静态 PE 偏空）**：成长股估值的核心是「未来增长能否消化当前估值」，\n"
                "   静态 PE_TTM 不得单独作为偏空结论的主要依据。估值分析必须优先采用：Future EPS / Forward PE / PEG / 利润增速 / 行业增速\n"
                "   （前瞻数据用 mx_data_query 查一致预期/业绩预告；get_fundamentals 已给 trailing PEG = pe_ttm/净利润同比增速 供参考）。\n"
                "   判定树：\n"
                "     成长股 -> 利润未来三年是否高速增长？\n"
                "       是 -> Forward PE 是否快速下降？ 是 -> PE 高不是问题（高 PE 合理）\n"
                "       否 -> PEG 是否 > 2？ 是 -> 估值开始危险\n"
                "   即：不是「PE=190 -> 危险」，而是「PE=190 -> 利润未来还能翻倍吗？-> 能 -> PE 不是核心问题」。\n"
                "   **只有当三者同时成立--利润增长明显放缓、且 Forward PE 仍极高、且 PEG 明显失衡--才能把估值作为主要空头证据；\n"
                "   否则高 PE 只能作为风险提示写入 evidence，不得据此偏空。**\n"
                "   ### 三、裁判结论\n"
                "   多空各自最硬的 1 条；互斥矛盾点 → 倾向哪边？为什么？\n"
                "   **必须包含加权四维评分**（按步骤 0 声明的权重）：\n"
                "   - 技术面 X 分 × Wt% = Y\n"
                "   - 基本面 X 分 × Wf% = Y\n"
                "   - 资金面 X 分 × Wm% = Y\n"
                "   - 情绪面 X 分 × Ws% = Y\n"
                "   - 加权总分 = Z → 档位\n"
                "   最终 verdict + initial_confidence (1-10)\n"
                "\n"
                "4) **置信度调整（一次性结算）**：以 initial_confidence 为基准，遍历下表逐条结算。\n"
                "   最终 final_confidence = clamp(initial − 扣减总和 + 加分总和, 1, 10)。\n"
                "   在裁判结论里逐条列出命中的规则与具体数额。\n"
                "\n"
                "   | 条件 | 调整 |\n"
                "   |---|---|\n"
                "   | 历史命中率 < 50% 或中位收益为负（历史 ≥ 3 次才触发；< 3 次改为 **−1**） | **−2**（≥3次）/ **−1**（<3次） |\n"
                "   | 每条「空头论点未被第三段有效反驳」 | **−1/条** |\n"
                "   | 加仓突破行业集中度（≥3 同行业）或总风险逼近上限 | **−1** |\n"
                "   | 多头判断 + 大盘弱势（沪深300 5日 < −2%） | **−1** |\n"
                "   | 多头判断 + 板块跑输大盘（5日 差 < −1%） | **−1** |\n"
                "   | 多头判断 + 个股跑输板块（5日 差 < −1.5%） | **−1** |\n"
                "   | 多头判断 + 板块强于大盘（5日 差 ≥ +1.5%） | **+1** |\n"
                "   | 多头判断 + 业绩超预期（最近季度净利润 yoy ≥ +30%）且 PE_TTM ≤ 30 | **+1** |\n"
"   | 成长股 + 偏空 + 估值作为主要依据但未引用 Forward PE / PEG / 一致预期数字 | **−2**（静态 PE 偏空误杀高成长标的） |\n"
                "   | 空头判断 + ST/退市风险 或 监管立案/处罚 | **+1** |\n"
                "   | 空头判断 + regime 弱势（任一 regime 利空命中） | 顺势，不扣不加 |\n"
                "   | **24h 内重复分析** | 系统自动限幅：方向 ±1 档 / conf ±2（防 LLM 随机漂移）。AI 如实给判断、不要自行压分；在 record_verdict 的 new_info 列出本次新增信息，系统据此判断是否豁免限幅 |\n"
                "   | **蓝筹/白马 + 基本面证据 < 2 条** | **−2**（基本面权重 50% 但没有实质证据，置信度必须打折扣） |\n"
                "   | **题材/游资 + 资金面证据缺失** | **−1**（资金面权重 30%，没有龙虎榜/主力流向数据则信号不完整） |\n"
                "\n"
                "5) 调 record_verdict：confidence 填 final_confidence；evidence ≥3 条，格式「数据点 → 推论」，引用真实数字。setup_tag 从种子词表（打板/首板/龙回头/板块轮动/超跌反弹/趋势突破/业绩驱动/题材炒作/低位反转）选最贴切本次驱动逻辑的一个，都不贴切填「其他:<自定义>」。\n"
                "   **stock_type 必填**：填步骤 0 声明的标的类型（蓝筹白马/题材游资/周期股/成长股/均衡型）。\n"
                "   **valuation_basis**（仅偏空类必填）：本次空头结论中「估值」是不是主要依据--\n"
                "   forward_valuation=基于 Forward PE/PEG/一致预期等前瞻估值（成长股偏空唯一允许的估值依据）/ static_pe_only=仅静态 PE_TTM / non_valuation=估值非主要依据。\n"
                "   **成长股 + 偏空 + valuation_basis=static_pe_only（或缺填）会被系统拒绝**，逼你补前瞻估值后重调。\n"
                "\n"
                "6) **自我检查（在调用 record_verdict 前完成，写在 message content 末尾）**：\n"
                "   - [ ] 我的四维权重与声明的股票类型是否一致？\n"
                "   - [ ] 蓝筹/成长股：基本面证据是否 ≥ 2 条且来自 mx_data_query 或博查 earnings？\n"
                "   - [ ] 题材/游资股：我是否错误地把\"基本面\"当成了主要判断依据？\n"
                "   - [ ] 周期股：我是否在 PE 很低时说\"估值便宜\"（这是周期股陷阱）？\n"
                "   - [ ] 成长股：我是否仅凭静态 PE_TTM 偏高就偏空？是否用 mx_data_query 查了一致预期/Forward PE/PEG？只有「增长放缓 + Forward PE 极高 + PEG 失衡」三者同时成立，估值才能作为主要空头依据，否则高 PE 只能是风险提示。\n"
"   - [ ] 我的 K 线分析深度是否与股票类型匹配（蓝筹股不需要逐根 K 线数浪）？\n"
                "   - [ ] setup_tag 是否反映了本次最核心的驱动逻辑（而非随便选一个）？\n"
                "   如果任一条不通过，回到对应步骤修正后再调 record_verdict。"
            ),
        },
    ]

    verdict_data: dict = {}
    position_action_data: dict = {}  # v1.1.0: 持仓路径捕获（与 verdict_data 互斥，对称守卫保证）
    analysis_text = ""
    iteration = 0
    searches_performed: list[str] = []  # 累计调用过的 web_search category

    while True:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            max_tokens=16384,
            temperature=0.4,
            extra_body={"thinking": {"type": "disabled"}},
        )

        choice = response.choices[0]
        msg = choice.message

        # Collect text content + emit as event
        if msg.content:
            analysis_text += msg.content
            _emit({
                "type": "assistant_text",
                "iteration": iteration,
                "content": msg.content,
            })

        # Check termination
        if choice.finish_reason != "tool_calls":
            if choice.finish_reason == "length":
                raise AnalysisError(f"AI exceeded token limit at iteration {iteration}")
            break

        if iteration >= max_iter:
            raise AnalysisError(f"Exceeded max tool iterations ({max_iter})")

        # Append assistant message (with tool_calls) to history
        messages.append(msg)

        # Execute all tool calls
        for tool_call in (msg.tool_calls or []):
            name = tool_call.function.name
            try:
                tool_input = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                tool_input = {}

            _emit({
                "type": "tool_call",
                "iteration": iteration,
                "tool_call_id": tool_call.id,
                "name": name,
                "args": tool_input,
            })

            if name == "record_verdict":
                # 校验 1: 强制博查类别必须全部调用过
                missing = [
                    c for c in data.MANDATORY_SEARCH_CATEGORIES
                    if c not in searches_performed
                ]
                # 校验 2: 看多类(看多/偏多/观望偏多)必须填具体价位,
                # entry/stop_loss/target 任一 ≤ 0 视为漏填(0 是非看多方向的占位)
                bad_prices = []
                if not missing and tool_input.get("verdict") in BULLISH_VERDICTS:
                    bad_prices = [
                        f for f in ("entry", "stop_loss", "target")
                        if not (
                            isinstance(tool_input.get(f), (int, float))
                            and tool_input.get(f) > 0
                        )
                    ]

                # 校验 3: 成长股偏空不得仅凭静态 PE（防误杀高成长标的）
                # 成长股 + 偏空 + valuation_basis 为 static_pe_only 或缺填 -> 拒绝，逼 AI 补前瞻估值后重调
                static_pe_bearish = False
                if (not missing and not bad_prices
                        and tool_input.get("stock_type") == "成长股"
                        and tool_input.get("verdict") in BEARISH_VERDICTS
                        and tool_input.get("valuation_basis") in (None, "static_pe_only")):
                    static_pe_bearish = True

                # 校验 4 (v1.1.0 对称守卫): 已持仓票必须走 record_position_action，禁 record_verdict
                held_conflict = False
                try:
                    from apex import watchlist as _wl_guard
                    _held_now = next(
                        (p for p in _wl_guard.load().get("active_positions", [])
                         if p.get("ts_code") == ts_code), None
                    )
                    held_conflict = _held_now is not None
                except Exception:
                    held_conflict = False  # 读失败不阻断（advisory，宁可放行不误杀）

                if missing or bad_prices or static_pe_bearish or held_conflict:
                    # 拒绝记录结论, 把错误喂回 AI 逼其修正后重调 record_verdict
                    reasons = []
                    if missing:
                        reasons.append(
                            f"强制博查类别未全部调用, 缺: {missing}。"
                            f"请先调用 web_search(category=<上述类别>) 补齐"
                        )
                    if bad_prices:
                        reasons.append(
                            f"方向为「{tool_input.get('verdict')}」属看多类, "
                            f"价位 {bad_prices} 必须填大于 0 的具体数字, 不能填 0"
                        )
                    if static_pe_bearish:
                        reasons.append(
                            f"标的类型为「成长股」且方向偏空，但 valuation_basis="
                            f"{tool_input.get('valuation_basis')!r}（仅静态 PE_TTM 或未填）。"
                            "成长股估值的核心是「未来增长能否消化当前估值」，静态 PE 不得单独作为偏空主要依据。"
                            "请用 mx_data_query 查一致预期/业绩预告，算 Forward PE / PEG 后重判："
                            "若「利润增长明显放缓 + Forward PE 仍极高 + PEG 明显失衡」三者同时成立，"
                            "改填 valuation_basis=forward_valuation 并在 evidence 引用前瞻数字；"
                            "若估值非主要空头依据，改填 valuation_basis=non_valuation。"
                        )
                    if held_conflict:
                        reasons.append(
                            f"你已持有 {ts_code}，重新分析已持仓票必须调 record_position_action"
                            "（给 hold/add/trim/exit 加减仓建议），record_verdict 会被拒绝。"
                        )
                    result = json.dumps({
                        "error": "；".join(reasons) + "。请修正后重新调用 record_verdict。",
                        "missing_categories": missing,
                        "bad_prices": bad_prices,
                        "static_pe_bearish": static_pe_bearish,
                        "held_conflict": held_conflict,
                        "performed": searches_performed,
                    }, ensure_ascii=False)
                    _emit({
                        "type": "verdict_rejected",
                        "iteration": iteration,
                        "tool_call_id": tool_call.id,
                        "missing": missing,
                        "bad_prices": bad_prices,
                        "static_pe_bearish": static_pe_bearish,
                        "held_conflict": held_conflict,
                        "performed": list(searches_performed),
                    })
                else:
                    verdict_data = tool_input
                    result = "verdict recorded"
                    _emit({
                        "type": "verdict_recorded",
                        "iteration": iteration,
                        "tool_call_id": tool_call.id,
                        "verdict": verdict_data.get("verdict"),
                        "confidence": verdict_data.get("confidence"),
                    })
            elif name == "record_position_action":
                # v1.1.0 持仓路径：守卫抽离到 _validate_position_action 供单测（T6）
                # ladder 路径模拟需现价锚定：实时价优先、日线收盘 fallback，都失败则降级（跳过路径检查）
                _cp_pa = None
                try:
                    _cp_pa = (data.get_realtime_price([ts_code]) or {}).get(ts_code)
                except Exception:
                    pass
                if _cp_pa is None:
                    try:
                        _cp_pa = (data.get_latest_price([ts_code]) or {}).get(ts_code)
                    except Exception:
                        pass
                pa_reasons, _sizing_warn = _validate_position_action(
                    tool_input, ts_code, searches_performed, current_price=_cp_pa
                )
                if pa_reasons:
                    result = json.dumps({
                        "error": "；".join(pa_reasons) + "。请修正后重新调用 record_position_action。",
                        "performed": list(searches_performed),
                    }, ensure_ascii=False)
                    _emit({
                        "type": "position_action_rejected",
                        "iteration": iteration,
                        "tool_call_id": tool_call.id,
                        "reasons": pa_reasons,
                    })
                else:
                    position_action_data = tool_input
                    result = ("position_action recorded" + (f" {_sizing_warn}" if _sizing_warn else "")).strip()
                    _emit({
                        "type": "position_action_recorded",
                        "iteration": iteration,
                        "tool_call_id": tool_call.id,
                        "action": position_action_data.get("action"),
                        "sizing_warn": _sizing_warn or None,
                    })
            else:
                if name == "web_search":
                    cat = tool_input.get("category", "general")
                    if cat not in searches_performed:
                        searches_performed.append(cat)
                try:
                    result = _dispatch_tool(name, tool_input)
                except Exception as e:
                    result = json.dumps({"error": str(e)})
                _emit({
                    "type": "tool_result",
                    "iteration": iteration,
                    "tool_call_id": tool_call.id,
                    "name": name,
                    "summary": trace_mod.summarize_tool_result(name, result),
                    "raw": result,
                })

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

        iteration += 1

        # Once a conclusion tool captured and no other pending tools, do one final text round
        if verdict_data or position_action_data:
            non_verdict = [tc for tc in (msg.tool_calls or [])
                           if tc.function.name not in ("record_verdict", "record_position_action")]
            if not non_verdict:
                final = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=2048,
                    temperature=0.4,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                final_text = final.choices[0].message.content
                if final_text:
                    analysis_text += "\n" + final_text
                    _emit({
                        "type": "assistant_text",
                        "iteration": iteration,
                        "content": final_text,
                        "final": True,
                    })
                break

    if not verdict_data and not position_action_data:
        # 兜底：AI 忘了调任一记录工具，给最后一次机会
        messages.append({
            "role": "user",
            "content": (
                "⚠️ 系统提醒：你还没有调用 record_verdict 函数来提交最终判断结论。\n"
                "纯文本分析不会被记录。请立即调用（二选一，不要再调其他工具）：\n"
                "- 未持仓票：record_verdict(verdict=..., confidence=..., evidence=[...])\n"
                "- 已持仓票：record_position_action(action=..., rationale=..., scale_plan=[...])"
            ),
        })
        retry = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            max_tokens=4096,
            temperature=0.4,
            extra_body={"thinking": {"type": "disabled"}},
        )
        retry_choice = retry.choices[0]
        retry_msg = retry_choice.message
        if retry_choice.finish_reason == "tool_calls":
            for tool_call in (retry_msg.tool_calls or []):
                _fname = tool_call.function.name
                if _fname in ("record_verdict", "record_position_action"):
                    try:
                        _captured = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        _captured = {}
                    if _fname == "record_verdict":
                        verdict_data = _captured
                        _emit({
                            "type": "verdict_recorded",
                            "iteration": iteration,
                            "tool_call_id": tool_call.id,
                            "verdict": verdict_data.get("verdict"),
                            "confidence": verdict_data.get("confidence"),
                        })
                    else:
                        position_action_data = _captured
                        _emit({
                            "type": "position_action_recorded",
                            "iteration": iteration,
                            "tool_call_id": tool_call.id,
                            "action": position_action_data.get("action"),
                            "sizing_warn": None,
                        })
                    break
        if not verdict_data and not position_action_data:
            raise AnalysisError("AI did not call record_verdict or record_position_action — no verdict captured")

    # ── v1.1.0 双路径分支：持仓路径直接收尾，不走 calibration/24h 限幅 ──
    if position_action_data:
        return _finalize_position_action(
            ts_code, position_action_data, analysis_text, events,
            playstyle_feats, market_ctx, save,
        )

    raw_verdict = verdict_data["verdict"]
    raw_confidence = verdict_data.get("confidence")
    # AI 自报置信度兜底：clamp 到 [1, 10]，避免扣减规则叠加把分扣穿
    if raw_confidence is not None:
        try:
            raw_confidence = max(1, min(10, int(raw_confidence)))
        except (TypeError, ValueError):
            raw_confidence = None

    # ── 24h 重复分析限幅（痛点#3：宁可错杀，防 LLM 随机漂移）──
    # 代码层强制：24h 内重复 + 无客观新增 -> 方向 ±1 档 / conf ±2。
    # prompt 已改成"系统自动限幅，AI 如实给判断"，避免双重限幅。
    repeat_info = _compute_repeat_analysis(
        history_entries,
        raw_verdict,
        raw_confidence,
        verdict_data.get("new_info", []),
        verdict_data.get("features", {}),
    )
    limited_verdict, limited_confidence = _apply_repeat_limit(
        repeat_info, raw_verdict, raw_confidence,
    )

    cal_score: Optional[float] = None
    cal_explanation = ""
    if limited_confidence is not None:
        try:
            cal_score, cal_explanation = calibration.calibrate_confidence(
                int(limited_confidence), limited_verdict,
            )
            if cal_score is not None:
                cal_score = max(1.0, min(10.0, float(cal_score)))
        except Exception as e:
            cal_explanation = f"校准失败: {e}"

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
        "verdict": limited_verdict,
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
        },
        "features": verdict_data.get("features", {}),
        "evidence": verdict_data.get("evidence", []),
        "searches_performed": searches_performed,
        "market_context": market_ctx,
        "analysis_text": analysis_text.strip(),
        "repeat_analysis": repeat_info,
        "prompt_version": "2.6.0",
        "source": "standalone",
        "setup_tag": verdict_data.get("setup_tag"),
        "stock_type": verdict_data.get("stock_type"),
        "valuation_basis": verdict_data.get("valuation_basis"),
        # ── Playstyle Engine v1（A2 持久化 FE 特征 / D13 risk_level / skill 分支 AI 填 playstyle / T6 契合度脚手架）──
        "playstyle": playstyle_value,                       # {ratings,primary,secondary,reasons,method,low_confidence} 或 null(FE<0.5)
        "playstyle_fit": playstyle_fit,                     # v1 恒 insufficient_data（D10，规则 v1.1）
        "playstyle_features": playstyle_feats,              # FE 10 特征 + completeness + risk_level（A2 可复现/可解释）
        "risk_level": playstyle_feats.get("risk_level"),    # low/medium/high，与 playstyle 正交（D13）
    }

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
        except (TypeError, ValueError):
            continue
        try:
            _nsf = float(_lvl.get("new_stop"))
        except (TypeError, ValueError):
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

    # 守卫 3 (OV#1): trim/exit 强制 regulatory+shareholders+money_flow 三类实质风险搜索
    _mandatory_pa = ["regulatory", "shareholders", "money_flow"]
    if action in ("trim", "exit") and _held_pa is not None:
        _missing_pa = [c for c in _mandatory_pa if c not in searches_performed]
        if _missing_pa:
            reasons.append(
                f"action={action} 涉及实质风险决策，强制搜索类别未全部调用，缺: {_missing_pa}。"
                "请先 web_search(category=<上述类别>) 补齐。"
            )

    # 守卫 4 (OV#7): 4h 反 churn -- 距上次 position_action <4h 且无 new_info -> 拒
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
    position_action_data: dict,
    analysis_text: str,
    events: list,
    playstyle_feats: dict,
    market_ctx: dict,
    save: bool,
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
        "source": POSITION_ACTION_SOURCE,
        "position_action": {
            "action": position_action_data.get("action"),
            "add_shares": position_action_data.get("add_shares"),
            "trim_shares": position_action_data.get("trim_shares"),
            "trim_pct": position_action_data.get("trim_pct"),
            "new_stop": position_action_data.get("new_stop"),
            "new_target": position_action_data.get("new_target"),
            "scale_plan": position_action_data.get("scale_plan") or [],
            "rationale": position_action_data.get("rationale"),
        },
        "analysis_text": analysis_text.strip(),
        "prompt_version": "2.6.0",
        "market_context": market_ctx,
        # P1: verdict/price_advice/features/evidence/confidence 全 None（不污染 calibration/backtest）
        "verdict": None,
        "confidence": None,
        "price_advice": None,
        "features": None,
        "evidence": None,
        # OV#3: playstyle 例外（股票属性，非方向 call 字段）
        "playstyle": playstyle_value,
        "playstyle_fit": playstyle_fit,
        "playstyle_features": playstyle_feats,
        "risk_level": playstyle_feats.get("risk_level"),
    }

    if save:
        journal.write_entry(entry)
        try:
            trace_mod.write_trace(ts_code, entry["analyzed_at"], events)
        except Exception as e:
            print(f"⚠ trace 写入失败（不影响 journal）: {e}")
        # 刷新持仓 ladder（竞态：分析期间平仓 -> plan 无处可写，journal 已落，下次重建）
        try:
            from apex import watchlist as _wl_fin
            _new_plan = {
                "scale_plan": entry["position_action"]["scale_plan"],
                "doctrine": "single_v1",
                "updated_at": entry["analyzed_at"],
                # B1 增强：最近 position_action 快照，供持仓卡显示"现在 vs 未来"
                # last_stop_before 由 update_plan 从持仓当前 stop_loss 补（race-free）
                "last_action": entry["position_action"]["action"],
                "last_new_stop": entry["position_action"]["new_stop"],
            }
            _updated = _wl_fin.update_plan(ts_code, _new_plan)
            if _updated is None:
                print(f"⚠ 持仓 {ts_code} 已不持仓，ladder 未写入（position_action journal 已保存）")
            else:
                _n = len(_new_plan["scale_plan"])
                print(f"✓ 已保存加减仓建议: {ts_code} -> {entry['position_action']['action']} (ladder {_n} 档)")
                # B1: new_stop 直接覆盖持仓 stop_loss（AI 建议即生效，不等 B3 sim 执行）。
                # update_plan 已把旧 stop 锁进 plan.last_stop_before，此处改 stop_loss 不影响 delta 展示。
                _new_stop = entry["position_action"].get("new_stop")
                if _new_stop is not None:
                    try:
                        _wl_fin.update_advice(ts_code, stop_loss=_new_stop, emit_notify=False)
                        print(f"✓ 已应用新止损: {ts_code} stop_loss -> {_new_stop}")
                    except _wl_fin.PositionNotFoundError:
                        print(f"⚠ 持仓 {ts_code} 竞态已平仓，新止损未应用")
                    except Exception as e:
                        print(f"⚠ 新止损应用失败（不影响 journal/plan）: {e}")
                # 同 new_stop：new_target 直接覆盖持仓 target（防化石止盈推送与 ladder 打架）。
                _new_target = entry["position_action"].get("new_target")
                if _new_target is not None:
                    try:
                        _wl_fin.update_advice(ts_code, target=_new_target, emit_notify=False)
                        print(f"✓ 已应用新止盈: {ts_code} target -> {_new_target}")
                    except _wl_fin.PositionNotFoundError:
                        print(f"⚠ 持仓 {ts_code} 竞态已平仓，新止盈未应用")
                    except Exception as e:
                        print(f"⚠ 新止盈应用失败（不影响 journal/plan）: {e}")
        except Exception as e:
            print(f"⚠ ladder 刷新失败（不影响 journal）: {e}")

    entry["_trace_events"] = events
    return entry
