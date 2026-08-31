"""Forecast-only calibration, independent from executed-trade performance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

from apex.decision_policy import POLICY_VERSION


FORECAST_HORIZON_DAYS = 10
EXCESS_DEAD_ZONE_PCT = 1.0
MIN_CALIBRATION_SAMPLES = 10
_CACHE_FILE = "forecast-outcomes-v1.json"
_BULLISH = {"看多", "偏多"}
_BEARISH = {"偏空", "看空"}
_NEUTRAL = {"观望偏多", "中性", "观望偏空"}


@dataclass(frozen=True)
class CalibrationResult:
    score: float
    sample_size: int
    applied: bool
    explanation: str


def classify_outcome(stock_return_pct: float, benchmark_return_pct: float) -> dict[str, Any]:
    excess = float(stock_return_pct) - float(benchmark_return_pct)
    outcome = "bull" if excess > EXCESS_DEAD_ZONE_PCT else "bear" if excess < -EXCESS_DEAD_ZONE_PCT else "neutral"
    return {
        "stock_return_pct": float(stock_return_pct),
        "benchmark_return_pct": float(benchmark_return_pct),
        "excess_return_pct": excess,
        "outcome": outcome,
    }


def forecast_class(verdict: str) -> str:
    if verdict in _BULLISH:
        return "bull"
    if verdict in _BEARISH:
        return "bear"
    if verdict in _NEUTRAL:
        return "neutral"
    raise ValueError(f"unknown verdict: {verdict}")


def is_forecast_hit(verdict: str, outcome: str) -> bool:
    return forecast_class(verdict) == outcome


def calibrate_confidence(
    model_confidence: int | float,
    verdict: str,
    forecast_rows: list[dict[str, Any]],
    *,
    min_samples: int = MIN_CALIBRATION_SAMPLES,
) -> CalibrationResult:
    target_class = forecast_class(verdict)
    eligible = [
        row for row in forecast_rows
        if row.get("policy_version") == POLICY_VERSION
        and row.get("hit") is not None
        and forecast_class(str(row.get("verdict") or "中性")) == target_class
    ]
    raw = max(1.0, min(10.0, float(model_confidence)))
    if len(eligible) < min_samples:
        return CalibrationResult(
            score=raw,
            sample_size=len(eligible),
            applied=False,
            explanation=f"预测样本不足（n={len(eligible)} < {min_samples}），使用模型原始置信度",
        )
    hit_rate = sum(bool(row.get("hit")) for row in eligible) / len(eligible)
    score = round(max(1.0, min(10.0, hit_rate * 10.0)), 1)
    return CalibrationResult(
        score=score,
        sample_size=len(eligible),
        applied=True,
        explanation=f"{target_class} 方向预测 {len(eligible)} 条，命中率 {hit_rate:.0%} → {score}/10",
    )


def _bars_by_date(rows: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        trade_date = str(row.get("trade_date") or "").replace("-", "")
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if trade_date and close > 0:
            result[trade_date] = close
    return result


def evaluate_matured_forecast(
    entry: dict[str, Any],
    stock_bars: list[dict[str, Any]],
    benchmark_bars: list[dict[str, Any]],
    *,
    horizon: int = FORECAST_HORIZON_DAYS,
) -> dict[str, Any] | None:
    """Evaluate the close-to-close return on the tenth shared trading session."""
    if entry.get("policy_version") != POLICY_VERSION:
        return None
    start = str(entry.get("date") or entry.get("analyzed_at") or "")[:10].replace("-", "")
    stock = _bars_by_date(stock_bars)
    benchmark = _bars_by_date(benchmark_bars)
    benchmark_dates = sorted(day for day in benchmark if day >= start)
    if len(benchmark_dates) <= horizon:
        return None
    entry_day = benchmark_dates[0]
    matured_day = benchmark_dates[horizon]
    if entry_day not in stock or matured_day not in stock:
        return None
    stock_return = (stock[matured_day] - stock[entry_day]) / stock[entry_day] * 100.0
    benchmark_return = (benchmark[matured_day] - benchmark[entry_day]) / benchmark[entry_day] * 100.0
    result = classify_outcome(stock_return, benchmark_return)
    result.update({
        "matured_at": f"{matured_day[:4]}-{matured_day[4:6]}-{matured_day[6:]}",
        "horizon_trading_days": horizon,
        "hit": is_forecast_hit(str(entry.get("verdict")), result["outcome"]),
        "verdict": entry.get("verdict"),
        "policy_version": POLICY_VERSION,
    })
    return result


def aggregate_matured_forecasts(
    entries: list[dict[str, Any]],
    *,
    stock_loader: Callable[[str, str, str], list[dict[str, Any]]],
    benchmark_loader: Callable[[str, str], list[dict[str, Any]]],
    today: str,
) -> list[dict[str, Any]]:
    """Build forecast-only outcomes without mutating historical journal rows."""
    eligible_rows = [
        entry for entry in entries
        if entry.get("analysis_status", "completed") == "completed"
        and entry.get("policy_version") == POLICY_VERSION
        and entry.get("verdict") is not None
        and entry.get("ts_code")
    ]
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in eligible_rows:
        key = (str(entry["ts_code"]), str(entry.get("date") or entry.get("analyzed_at") or "")[:10])
        previous = deduped.get(key)
        if previous is None or str(entry.get("analyzed_at") or "") >= str(previous.get("analyzed_at") or ""):
            deduped[key] = entry
    eligible = list(deduped.values())
    if not eligible:
        return []
    start = min(str(entry.get("date") or entry.get("analyzed_at") or "")[:10] for entry in eligible)
    benchmark_bars = benchmark_loader(start, today)
    outcomes: list[dict[str, Any]] = []
    stock_cache: dict[str, list[dict[str, Any]]] = {}
    for entry in eligible:
        code = str(entry["ts_code"])
        if code not in stock_cache:
            stock_cache[code] = stock_loader(code, start, today)
        outcome = evaluate_matured_forecast(entry, stock_cache[code], benchmark_bars)
        if outcome is not None:
            outcomes.append({
                **outcome,
                "ts_code": code,
                "analyzed_at": entry.get("analyzed_at") or entry.get("date"),
            })
    return outcomes


def _cache_path() -> Path:
    from apex import config

    return Path(config.get()["paths"]["journal_dir"]) / _CACHE_FILE


def load_forecast_rows() -> list[dict[str, Any]]:
    try:
        payload = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError):
        return []
    return list(payload) if isinstance(payload, list) else []


def refresh_forecast_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refresh the forecast cache; provider failures preserve the last good cache."""
    eligible = [entry for entry in entries if entry.get("policy_version") == POLICY_VERSION]
    if not eligible:
        return load_forecast_rows()
    try:
        from apex import data, market_cache

        def stock_loader(code: str, start: str, end: str) -> list[dict[str, Any]]:
            rows = list(json.loads(data.get_daily_price(code, start, end)))
            if not rows:
                raise ValueError(f"empty stock bars: {code}")
            return rows

        def benchmark_loader(start: str, end: str) -> list[dict[str, Any]]:
            series = market_cache.load_index_daily("000300.SH", start, end)
            rows = [
                {"trade_date": index.strftime("%Y%m%d"), "close": float(value)}
                for index, value in series.items()
            ]
            if not rows:
                raise ValueError("empty benchmark bars: 000300.SH")
            return rows

        rows = aggregate_matured_forecasts(
            eligible,
            stock_loader=stock_loader,
            benchmark_loader=benchmark_loader,
            today=date.today().isoformat(),
        )
        previous = load_forecast_rows()
        if not rows and previous:
            return previous
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{_CACHE_FILE}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return rows
    except Exception:
        return load_forecast_rows()
