"""Signal fetchers — each module exposes fetch(trade_date: str) -> list[SignalRecord]."""
from apex.signals import (
    dragon_tiger,
    limit_up,
    industry,
    northbound,
    concept,
    limit_up_history,
)

FETCHERS = {
    "dragon_tiger": dragon_tiger.fetch,
    "limit_up": limit_up.fetch,
    "industry": industry.fetch,
    "northbound": northbound.fetch,
    "concept": concept.fetch,
    "limit_up_history": limit_up_history.fetch,
}
