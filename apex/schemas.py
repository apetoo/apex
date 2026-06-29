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
