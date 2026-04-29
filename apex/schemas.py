from typing import TypedDict, Optional, Literal

VERDICT_ENUM = ["看多", "偏多", "观望偏多", "中性", "观望偏空", "偏空", "看空"]
BULLISH_VERDICTS = {"看多", "偏多", "观望偏多"}
BEARISH_VERDICTS = {"看空", "偏空", "观望偏空"}

REQUIRED_FEATURE_KEYS = ["ma5_position", "ma20_position", "volume_ratio", "rsi_14"]


class FeaturesSchema(TypedDict, total=False):
    ma5_position: Literal["above", "below"]
    ma20_position: Literal["above", "below"]
    ma_alignment: Literal["bullish", "bearish", "mixed"]
    volume_ratio: float
    macd_zone: Literal["above_zero", "below_zero"]
    rsi_14: float
    price_vs_ma5_pct: float


class PriceAdviceSchema(TypedDict, total=False):
    entry: Optional[float]
    stop_loss: Optional[float]
    target: Optional[float]


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
