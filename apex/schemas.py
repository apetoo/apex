from typing import TypedDict, Optional, Literal

VERDICT_ENUM = ["看多", "偏多", "观望偏多", "中性", "观望偏空", "偏空", "看空"]
BULLISH_VERDICTS = {"看多", "偏多", "观望偏多"}
BEARISH_VERDICTS = {"看空", "偏空", "观望偏空"}

REQUIRED_FEATURE_KEYS = ["ma5_position", "ma20_position", "volume_ratio", "rsi_14"]

SIGNAL_TYPES = ["dragon_tiger", "limit_up", "industry", "northbound", "concept", "limit_up_history", "moneyflow"]

EXIT_REASON_ENUM = ["stop_hit", "target_hit", "manual", "expired", "other"]


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
