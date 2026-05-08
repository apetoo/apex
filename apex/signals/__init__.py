"""Signal fetchers — each module exposes fetch(trade_date: str) -> list[SignalRecord]."""
from apex.signals import (
    dragon_tiger,
    limit_up,
    industry,
    northbound,
    concept,
)

FETCHERS = {
    "dragon_tiger": dragon_tiger.fetch,
    "limit_up": limit_up.fetch,
    "industry": industry.fetch,
    "northbound": northbound.fetch,
    "concept": concept.fetch,
}
