"""Controlled representative-constituent selection for Eastmoney Guba targets."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Callable

MembershipFetcher = Callable[[dict], tuple[list[dict], str]]
TurnoverFetcher = Callable[[list[str], str, int], tuple[dict[str, float], str, str]]


def _default_membership_fetcher(sector: dict) -> tuple[list[dict], str]:
    import akshare as ak

    name = str(sector.get("sector_name") or "").strip()
    if not name:
        raise ValueError("sector name is required")
    if sector.get("taxonomy") == "industry":
        method = "stock_board_industry_cons_em"
    else:
        method = "stock_board_concept_cons_em"
    frame = getattr(ak, method)(symbol=name)
    if frame is None or frame.empty or "代码" not in frame.columns:
        raise ValueError("sector membership is unavailable")
    rows = [{"stock_code": str(value).strip()} for value in frame["代码"].tolist()]
    return rows, f"akshare.{method}"


def _default_turnover_fetcher(
    stock_codes: list[str], trade_date: str, trading_days: int,
) -> tuple[dict[str, float], str, str]:
    from apex import data

    api = data._tushare()
    end = date.fromisoformat(trade_date)
    start = end - timedelta(days=max(40, trading_days * 3))
    calendar = api.trade_cal(
        exchange="", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
        is_open="1", fields="cal_date,is_open",
    )
    if calendar is None or calendar.empty or "cal_date" not in calendar.columns:
        raise ValueError("trading calendar is unavailable")
    dates = sorted(str(value) for value in calendar["cal_date"].tolist())[-trading_days:]
    if len(dates) < trading_days:
        raise ValueError("fewer than twenty trading dates are available")
    normalized = {data.normalize_ts_code(code): code for code in stock_codes}
    totals = {code: 0.0 for code in stock_codes}
    observations = {code: 0 for code in stock_codes}
    for trading_date in dates:
        frame = api.daily(
            trade_date=trading_date, fields="ts_code,trade_date,amount",
        )
        if frame is None or frame.empty:
            continue
        for row in frame.itertuples(index=False):
            original = normalized.get(data.normalize_ts_code(str(row.ts_code)))
            if original is None:
                continue
            amount = float(row.amount)
            if math.isfinite(amount) and amount >= 0:
                totals[original] += amount
                observations[original] += 1
    totals = {code: value for code, value in totals.items()
              if observations[code] == trading_days}
    as_of = date.fromisoformat(f"{dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:]}").isoformat()
    return totals, as_of, "tushare.pro.daily.amount"


def select_representative_constituents(
    taxonomy: list[dict], trade_date: str, constituent_count: int = 5, *,
    membership_fetcher: MembershipFetcher | None = None,
    turnover_fetcher: TurnoverFetcher | None = None,
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """Select deterministic turnover leaders and return frozen per-sector provenance."""
    if isinstance(constituent_count, bool) or constituent_count < 0:
        raise ValueError("constituent_count must be a non-negative integer")
    membership_fetcher = membership_fetcher or _default_membership_fetcher
    turnover_fetcher = turnover_fetcher or _default_turnover_fetcher
    constituents: dict[str, list[dict]] = {}
    provenance: dict[str, dict] = {}
    for sector in taxonomy:
        sector_id = str(sector["sector_id"])
        try:
            members, membership_source = membership_fetcher(sector)
            codes = []
            seen = set()
            for row in members:
                code = str(row.get("stock_code") or "").strip()
                if code and code not in seen:
                    seen.add(code)
                    codes.append(code)
            turnover, as_of, turnover_source = turnover_fetcher(codes, trade_date, 20)
            ranked = []
            for code in codes:
                value = turnover.get(code)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                value = float(value)
                if not math.isfinite(value) or value < 0:
                    continue
                ranked.append({
                    "stock_code": code, "turnover_20d": value,
                    "turnover_as_of": str(as_of),
                    "membership_source": str(membership_source),
                    "turnover_source": str(turnover_source),
                })
            ranked.sort(key=lambda row: (-row["turnover_20d"], row["stock_code"]))
            selected = ranked[:constituent_count]
            status = "ok" if len(selected) == constituent_count else "insufficient"
            detail = {
                "sector_id": sector_id, "membership_source": str(membership_source),
                "turnover_source": str(turnover_source), "turnover_as_of": str(as_of),
                "lookback_trading_days": 20, "requested_count": constituent_count,
                "selected_count": len(selected), "selected": selected, "status": status,
            }
            if status != "ok":
                detail["reason"] = (
                    f"only {len(selected)} valid constituents have twenty-day turnover"
                )
            constituents[sector_id] = selected
            provenance[sector_id] = detail
        except Exception as exc:
            constituents[sector_id] = []
            provenance[sector_id] = {
                "sector_id": sector_id, "membership_source": "unavailable",
                "turnover_source": "unavailable", "turnover_as_of": None,
                "lookback_trading_days": 20, "requested_count": constituent_count,
                "selected_count": 0, "selected": [], "status": "failed",
                "reason": f"representative provider failed: {type(exc).__name__}",
            }
    return constituents, provenance


def describe_injected_constituents(
    taxonomy: list[dict], constituents: dict[str, list[dict]], *,
    trade_date: str, constituent_count: int,
) -> dict[str, dict]:
    """Normalize legacy injected mappings into the same auditable manifest contract."""
    output = {}
    for sector in taxonomy:
        sector_id = str(sector["sector_id"])
        selected = _with_row_sources(
            constituents.get(sector_id, []), membership_source="injected",
            turnover_source="injected.turnover_20d", turnover_as_of=trade_date,
            constituent_count=constituent_count,
        )
        status = "ok" if len(selected) == constituent_count else "insufficient"
        detail = {
            "sector_id": sector_id, "membership_source": "injected",
            "turnover_source": "injected.turnover_20d", "turnover_as_of": trade_date,
            "lookback_trading_days": 20, "requested_count": constituent_count,
            "selected_count": len(selected), "selected": selected, "status": status,
        }
        if status != "ok":
            detail["reason"] = f"injected provider selected {len(selected)} of {constituent_count}"
        output[sector_id] = detail
    return output


def normalize_injected_representative_provenance(
    taxonomy: list[dict], constituents: dict[str, list[dict]], provenance: dict, *,
    trade_date: str, constituent_count: int,
) -> dict[str, dict]:
    """Make legacy tuple-provider provenance complete and consistent with its rows."""
    output: dict[str, dict] = {}
    for sector in taxonomy:
        sector_id = str(sector["sector_id"])
        rows = constituents.get(sector_id, [])
        raw_selected = list(rows)[:constituent_count] if isinstance(rows, list) else []
        supplied = provenance.get(sector_id)
        if not isinstance(supplied, dict):
            selected = _with_row_sources(
                raw_selected, membership_source="injected",
                turnover_source="injected.turnover_20d", turnover_as_of=trade_date,
                constituent_count=constituent_count,
            )
            output[sector_id] = {
                "sector_id": sector_id, "membership_source": "injected",
                "turnover_source": "injected.turnover_20d", "turnover_as_of": trade_date,
                "lookback_trading_days": 20, "requested_count": constituent_count,
                "selected_count": len(selected), "selected": selected, "status": "failed",
                "reason": "injected_provenance_missing",
            }
            continue
        detail = dict(supplied)
        count_matches = supplied.get("selected_count") == len(raw_selected)
        rows_match = _same_selected_rows(supplied.get("selected"), raw_selected)
        requested_matches = supplied.get("requested_count") == constituent_count
        supplied_status = str(supplied.get("status") or "")
        membership_source = str(supplied.get("membership_source") or "injected")
        turnover_source = str(supplied.get("turnover_source") or "injected.turnover_20d")
        turnover_as_of = supplied.get("turnover_as_of") or trade_date
        selected = _with_row_sources(
            raw_selected, membership_source=membership_source, turnover_source=turnover_source,
            turnover_as_of=turnover_as_of, constituent_count=constituent_count,
        )
        detail.update({
            "sector_id": sector_id,
            "membership_source": membership_source,
            "turnover_source": turnover_source,
            "turnover_as_of": turnover_as_of,
            "lookback_trading_days": 20,
            "requested_count": constituent_count,
            "selected_count": len(selected),
            "selected": selected,
        })
        if supplied_status == "failed":
            detail["status"] = "failed"
            detail.setdefault("reason", "injected_provider_failed")
        elif len(selected) < constituent_count:
            detail["status"] = "insufficient"
            detail.setdefault("reason", "injected_constituent_shortfall")
        elif not (count_matches and rows_match and requested_matches) or supplied_status != "ok":
            detail["status"] = "failed"
            detail["reason"] = "injected_provenance_inconsistent"
        else:
            detail["status"] = "ok"
        output[sector_id] = detail
    return output


def _with_row_sources(
    rows: object, *, membership_source: str, turnover_source: str, turnover_as_of: str,
    constituent_count: int,
) -> list[dict]:
    if not isinstance(rows, list):
        return []
    selected = []
    for row in rows[:constituent_count]:
        if not isinstance(row, dict):
            continue
        value = dict(row)
        value["membership_source"] = membership_source
        value["turnover_source"] = turnover_source
        value.setdefault("turnover_as_of", turnover_as_of)
        selected.append(value)
    return selected


def _same_selected_rows(supplied: object, rows: list[dict]) -> bool:
    if not isinstance(supplied, list) or len(supplied) != len(rows):
        return False
    def base(value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {key: item for key, item in value.items()
                if key not in {"membership_source", "turnover_source", "turnover_as_of"}}
    return [base(value) for value in supplied] == [base(value) for value in rows]


def describe_provider_failure(
    taxonomy: list[dict], *, trade_date: str, constituent_count: int,
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """Create safe, auditable per-sector shortfalls after provider unavailability."""
    constituents = {str(sector["sector_id"]): [] for sector in taxonomy}
    provenance = {
        sector_id: {
            "sector_id": sector_id, "membership_source": "unavailable",
            "turnover_source": "unavailable", "turnover_as_of": None,
            "lookback_trading_days": 20, "requested_count": constituent_count,
            "selected_count": 0, "selected": [], "status": "failed",
            "reason": "representative_provider_unavailable",
        }
        for sector_id in constituents
    }
    return constituents, provenance
