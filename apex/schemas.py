from typing import TypedDict, Optional, Literal

VERDICT_ENUM = ["看多", "偏多", "观望偏多", "中性", "观望偏空", "偏空", "看空"]
BULLISH_VERDICTS = {"看多", "偏多", "观望偏多"}
BEARISH_VERDICTS = {"看空", "偏空", "观望偏空"}

# 步骤 0 AI 声明的标的类型（与 analyze.py 类型表对齐）。成长股触发静态 PE 偏空硬门控。
STOCK_TYPE_ENUM = ["蓝筹白马", "题材游资", "周期股", "成长股", "均衡型"]

# 偏空类空头结论中「估值」是不是主要依据的分类（防成长股静态 PE 误杀）：
#   forward_valuation = 基于 Forward PE / PEG / 一致预期等前瞻估值（成长股偏空唯一允许的估值依据）
#   static_pe_only    = 仅静态 PE_TTM（成长股偏空会被系统拒绝）
#   non_valuation     = 估值非主要依据
VALUATION_BASIS_ENUM = ["forward_valuation", "static_pe_only", "non_valuation"]

REQUIRED_FEATURE_KEYS = ["ma5_position", "ma20_position", "volume_ratio", "rsi_14"]

SIGNAL_TYPES = ["dragon_tiger", "limit_up", "industry", "northbound", "concept", "limit_up_history", "moneyflow"]

EXIT_REASON_ENUM = ["stop_hit", "target_hit", "manual", "expired", "other"]

# 「我的交易系统」层（ADR-0001）：Setup 是用户声明的交易原型维度，与 strategy（决策来源）正交。
# 种子词表可扩展（用户可加自定义）；AI 在 record_verdict 时预填，用户在 candidate/promote 时确认/覆盖。
SETUP_SEED = [
    "打板", "首板", "龙回头", "板块轮动", "超跌反弹",
    "趋势突破", "业绩驱动", "题材炒作", "低位反转",
]

# Rule 检查表项词表（结构化、可自动评分）。每项 key 对应一个可观测的纪律条件。
# params 存参数（如 entry_band 的容差、sizing_cap 的百分比），checked 三态：True/False/None(未覆盖)。
RULE_ITEMS = [
    "entry_band",        # 入仓价在 AI plan entry ± 容差内
    "stop_formula",      # 止损按规则设置（如 fill-1.5×ATR）
    "sizing_cap",        # 单票仓位 ≤ 上限
    "no_average_down",   # 不加仓于亏损
    "max_hold_days",     # 持仓 ≤ 上限天数
    "sector_conc_cap",   # 板块集中度 ≤ 上限
    "no_chase",          # 不追高（入场不在 >X% 涨幅后）
]


class SignalRecord(TypedDict, total=False):
    ts_code: str
    name: str
    signal_type: str
    signal_strength: float
    raw: dict


class ScreenerEntry(TypedDict, total=False):
    ts_code: str
    name: str
    signals: list
    signal_types: list
    rule_score: float
    ai_score: Optional[int]
    verdict: Optional[str]
    one_liner: Optional[str]
    red_flag: Optional[bool]


class FeaturesSchema(TypedDict, total=False):
    ma5_position: Literal["above", "below"]
    ma20_position: Literal["above", "below"]
    ma_alignment: Literal["bullish", "bearish", "mixed"]
    volume_ratio: float
    macd_zone: Literal["above_zero", "below_zero"]
    rsi_14: float
    price_vs_ma5_pct: float
    atr_14_pct: float


class PriceAdviceSchema(TypedDict, total=False):
    entry: Optional[float]
    entry_low: Optional[float]
    entry_high: Optional[float]
    stop_loss: Optional[float]
    target: Optional[float]
    position_size_pct: Optional[int]


class RuleChecklistItemSchema(TypedDict, total=False):
    """单条 Rule 检查项（ADR-0001 自录层）。"""
    key: str          # RULE_ITEMS 之一，或自定义
    params: dict      # 参数（容差/百分比/天数等）
    checked: Optional[bool]  # True=遵守 / False=违规 / None=未覆盖（无法判定）


class RuleChecklistSchema(TypedDict, total=False):
    """用户在 promote 时提交的 Rule 检查表 + 自由备注。结构化项驱动自动守规评分，
    note 纳结构装不下的规则。随持仓写入 closed.open，供 close 后守规算分。"""
    items: list
    note: str


class RepeatAnalysisSchema(TypedDict, total=False):
    """24h 重复分析限幅记录（痛点#3透明化）。

    记录本次分析相比上次是否触发限幅、AI 原始值 vs 限幅后值，
    让用户能分辨 confidence 变化是 AI 改主意 / 24h 规则限幅 / 校准。
    """
    is_repeat: bool                  # 距上次分析 < 24h（客观硬条件）
    hours_since_last: float
    last_verdict: str
    last_confidence: int
    last_analyzed_at: str
    verdict_delta: int               # 方向变化档数（VERDICT_ENUM index 差，正=向空）
    confidence_delta: int
    new_info_claimed: list           # AI 自报本次新增信息
    new_info_verified: bool          # 客观交叉验证：技术面是否真的变了
    limited: bool                    # 是否触发了代码层限幅
    limit_rule: Optional[str]        # 限幅规则标签（verdict_delta_clamped / conf_clamped / ...）
    raw_verdict: str                 # AI 原始方向（限幅前）
    raw_confidence: int              # AI 原始置信度（限幅前）


class JournalEntry(TypedDict, total=False):
    ts_code: str
    date: str
    verdict: str
    confidence: int
    price: float
    price_advice: PriceAdviceSchema
    features: FeaturesSchema
    prompt_version: str
    source: str
    analysis_text: str
    repeat_analysis: RepeatAnalysisSchema
    setup_tag: Optional[str]   # ADR-0001: AI 预填的交易原型（SETUP_SEED 之一或自定义），供 candidate/promote 继承
    stock_type: Optional[str]  # 步骤 0 声明的标的类型（STOCK_TYPE_ENUM 之一），驱动成长股静态 PE 偏空门控
    valuation_basis: Optional[str]  # 偏空类空头估值依据分类（VALUATION_BASIS_ENUM 之一），审计用
