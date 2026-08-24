from typing import TypedDict, Optional, Literal


class EvidenceItem(TypedDict):
    id: str
    fact: str
    inference: str
    evidence_type: str
    tool_name: str
    source_name: str
    source_url: Optional[str]
    published_at: Optional[str]
    source_tier: int
    entity_matched: bool
    freshness_status: str

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

# Playstyle Engine v1 玩法 4 档（D9 prototype -> skill 分支：AI 填 ratings/primary/secondary）。
# schema 是 list，可折叠（Open Q #1）。同步自 apex/playstyle.py PLAYSTYLES。
PLAYSTYLE_ENUM = ["打野", "波段", "中线", "长线"]

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


# 持仓生命周期建议层（v1.1.0 B1）：已持仓票重新分析时, AI 产出加减仓建议而非重入场 verdict。
# position_action 是独立 journal record kind（source=POSITION_ACTION_SOURCE）, 不污染 calibration/backtest
# （calibration 评分入场方向 call、backtest 吃 bullish 入场信号, "加 300 股"既非方向 call 也非入场信号）。
POSITION_ACTION_SOURCE = "position_action"

# ladder 单档：加仓/减仓触发计划的一级。B3 sim 在每日 OHLC 上撮合, executed 标记防同档每 bar 重触发。
# 一根 bar 最多执行一个动作（优先级 stop_loss exit > trim > add）, 执行价 = trigger_price（对齐 auto-loop fill_price=trigger）。
class ScalePlanItemSchema(TypedDict, total=False):
    level: int                    # 1=首加/首减, 2=二加...
    trigger_price: float          # 触发价; B3 执行价 = 此值
    action: Literal["add", "trim"]
    shares: Optional[int]         # 加仓/减仓股数
    pct: Optional[float]          # 减仓比例 0-1（trim 时 shares/pct 二选一）
    new_stop: Optional[float]     # 触发后止损上移到（advisory, B1 只建议）
    reason: str
    executed: Optional[bool]      # B3 触发后标记 True; 缺省视为 False（未触发）


class PositionActionSchema(TypedDict, total=False):
    """已持仓票的加减仓建议（source=POSITION_ACTION_SOURCE 的 journal record）。

    action=add 必填 add_shares>0; action=trim 二选一 trim_shares/trim_pct;
    action=exit/hold 可只给 new_stop。scale_plan 给完整 ladder; rationale 是机器可读摘要,
    AI 推理全文进 analysis_text。B1 不含 rule_guards（无规则引擎, 字段恒空是死 schema, B2 规则引擎落地时加回）。
    """
    action: Literal["hold", "add", "trim", "exit"]
    add_shares: Optional[int]     # action=add 必填, >0
    trim_shares: Optional[int]    # action=trim 二选一
    trim_pct: Optional[float]     # action=trim 二选一（0-1）
    new_stop: Optional[float]     # 止损上移建议（add/hold 常带）
    new_target: Optional[float]   # 止盈价上移建议（同步 target 字段，防化石止盈与 ladder 冲突）
    scale_plan: list              # ScalePlanItemSchema[], 完整 ladder 计划
    rationale: str                # 机器可读摘要


class PositionPlanSchema(TypedDict, total=False):
    """持仓 ladder 快照（存于 active_positions.plan）。生于开仓（空 ladder, 锚定当前 stop/target）,
    每次重新分析 AI 演进（非替换）, 死于平仓（close_position null 掉防同 ts_code 重开读陈旧）。B3 sim 消费执行。"""
    scale_plan: list              # ScalePlanItemSchema[]
    doctrine: str                 # B1 单一默认 'single_v1'; B2 按 playstyle 分桶
    updated_at: Optional[str]     # 最后一次 position_action 刷新时间
    # B1 增强：最近一次 position_action 快照（供持仓卡显示"现在 vs 未来"，区分当前决策与条件触发计划）
    last_action: Optional[str]        # hold/add/trim/exit
    last_new_stop: Optional[float]    # AI 建议新止损（None=不动）
    last_stop_before: Optional[float] # position_action 时的旧止损（update_plan 从持仓 stop_lock 补, race-free）


class JournalEntry(TypedDict, total=False):
    ts_code: str
    analysis_status: Literal["completed", "insufficient_evidence"]
    unknowns: list[str]
    research_summary: str
    outcome_reason: Optional[str]
    next_actions: list[str]
    research_metrics: dict
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
    position_action: Optional[PositionActionSchema]  # v1.1.0: 已持仓票加减仓建议（source=POSITION_ACTION_SOURCE kind; verdict/price_advice/features 全 None）
