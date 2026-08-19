"""Read/write ~/.stock-journal/*.jsonl entries."""
import json
import os
import re
from pathlib import Path
from typing import Optional

from apex import config
from apex.schemas import VERDICT_ENUM, REQUIRED_FEATURE_KEYS, JournalEntry, POSITION_ACTION_SOURCE


def _journal_dir() -> Path:
    return Path(config.get()["paths"]["journal_dir"])


# 白名单: 只认 NNNNNN.SS.jsonl 形式的个股分析文件（如 603019.SH.jsonl）。
# 同目录下的 trades.jsonl / triggers.jsonl / *.trace.jsonl 等非分析文件会被
# glob("*.jsonl") 误读成判决, 污染 load_entries → 回测/calibration/health 全局统计。
# 用白名单而非黑名单(trades/triggers), 一劳永逸挡住未来新增的任何非分析 jsonl。
_ENTRY_NAME = re.compile(r"^\d{6}\.(SH|SZ|BJ)\.jsonl$")


def _entry_path(ts_code: str) -> Path:
    return _journal_dir() / f"{ts_code}.jsonl"


def validate_entry(entry: dict) -> dict:
    """Fill missing required feature keys with None; raise on invalid verdict."""
    verdict = entry.get("verdict")
    if verdict and verdict not in VERDICT_ENUM:
        raise ValueError(f"Invalid verdict '{verdict}'. Must be one of: {VERDICT_ENUM}")
    # features 可能为 None（position_action entry，P1 verdict 类字段全 None）-> 视作空 dict 填充
    features = entry.get("features") or {}
    for key in REQUIRED_FEATURE_KEYS:
        if key not in features:
            features[key] = None
    entry["features"] = features
    _fill_playstyle_fields(entry)
    return entry


# Playstyle Engine v1 字段（pre-v9 entry 无这些键，读/写时 lazy-fill None，append-only 不迁移 jsonl）
_PLAYSTYLE_FIELDS = ("playstyle", "playstyle_fit", "playstyle_features", "risk_level")


def _fill_playstyle_fields(entry: dict) -> dict:
    """为 pre-v9 历史 entry 补 Playstyle Engine v1 字段为 None（读时 lazy fill）。"""
    for k in _PLAYSTYLE_FIELDS:
        entry.setdefault(k, None)
    if "analysis_status" not in entry and (
        entry.get("verdict") is not None or entry.get("source") == POSITION_ACTION_SOURCE
    ):
        entry["analysis_status"] = "completed"
    return entry


def write_entry(entry: JournalEntry) -> None:
    ts_code = entry["ts_code"]
    entry = validate_entry(dict(entry))
    path = _entry_path(ts_code)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_entries(ts_code: Optional[str] = None) -> list[dict]:
    """Load all journal entries. If ts_code given, load only that stock."""
    journal_dir = _journal_dir()
    entries = []

    if ts_code:
        paths = [_entry_path(ts_code)]
    else:
        # 白名单: 只读 NNNNNN.SS.jsonl 个股分析文件。同目录的 trades.jsonl /
        # triggers.jsonl / *.trace.jsonl 等非分析文件不读, 否则会被当成判决污染
        # list_all_journal / health.check_journal / evidence_attribution / 回测。
        paths = sorted(
            p for p in journal_dir.glob("*.jsonl")
            if _ENTRY_NAME.match(p.name)
        )

    for path in paths:
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    _fill_playstyle_fields(entry)  # pre-v9 entry 补 playstyle 字段
                    entries.append(entry)

    return entries


def load_latest(ts_code: str) -> Optional[dict]:
    """Return most recent journal entry for a stock."""
    entries = load_entries(ts_code)
    if not entries:
        return None
    return sorted(entries, key=lambda e: e.get("analyzed_at") or e.get("date", ""))[-1]


def load_verdicts(ts_code: Optional[str] = None) -> list[dict]:
    """入场 verdict 记录（source != position_action）。

    供方向 call 语义消费点：calibration/backtest/repeat-analysis/下单上下文反查/
    ATR 估算手数/候选同步。position_action 记录 verdict=None，混入会让这些消费点静默断
    （None verdict 不命中 BULLISH/BEARISH、_verdict_index 返回 -1、price_advice 缺失）。
    **历史展示（/journal、/analyze 历史列表）用 load_entries（含 position_action），不用本函数。**
    """
    return [
        e for e in load_entries(ts_code)
        if e.get("source") != POSITION_ACTION_SOURCE
        and e.get("analysis_status", "completed") == "completed"
        and e.get("verdict") is not None
    ]


def load_position_actions(ts_code: Optional[str] = None) -> list[dict]:
    """持仓加减仓建议记录（source == position_action），按 analyzed_at 正序。

    供 position_action 路径反查（4h 反 churn 基线 / PositionCard 展示 / B3 sim 消费）。
    """
    return [
        e for e in load_entries(ts_code)
        if e.get("source") == POSITION_ACTION_SOURCE
        and e.get("analysis_status", "completed") == "completed"
        and e.get("position_action") is not None
    ]


def load_latest_verdict(ts_code: str) -> Optional[dict]:
    """最近一条入场 verdict（排除 position_action）。

    "latest journal = latest AI direction view" 场景专用：下单上下文反查、ATR 估算手数、
    候选价位同步、24h 限幅基线。持仓后最新一条可能是 position_action（无 verdict），
    这些场景必须取最近 verdict 而非最近 entry。
    """
    verdicts = load_verdicts(ts_code)
    if not verdicts:
        return None
    return sorted(verdicts, key=lambda e: e.get("analyzed_at") or e.get("date", ""))[-1]


_LEGACY_NAME = re.compile(r"^(\d{6})\.jsonl$")


def merge_legacy_files() -> dict:
    """
    One-shot: merge legacy unsuffixed files (e.g., 603019.jsonl) into the
    suffixed file (603019.SH.jsonl). Each line's ts_code field is normalized too.
    Old file is renamed to *.jsonl.legacy as a safety backup. Idempotent.
    Returns {"merged": N, "lines": M}.
    """
    from apex import data as _data

    journal_dir = _journal_dir()
    if not journal_dir.exists():
        return {"merged": 0, "lines": 0}

    stats = {"merged": 0, "lines": 0}
    for path in sorted(journal_dir.glob("*.jsonl")):
        m = _LEGACY_NAME.match(path.name)
        if not m:
            continue
        bare_code = m.group(1)
        suffixed = _data.normalize_ts_code(bare_code)
        if suffixed == bare_code:
            continue  # could not infer suffix
        target = journal_dir / f"{suffixed}.jsonl"

        with open(path, encoding="utf-8") as fin:
            lines = []
            for line in fin:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("ts_code") in (bare_code, ""):
                    rec["ts_code"] = suffixed
                lines.append(json.dumps(rec, ensure_ascii=False))

        if lines:
            with open(target, "a", encoding="utf-8") as fout:
                for line in lines:
                    fout.write(line + "\n")
            stats["lines"] += len(lines)

        path.rename(path.with_suffix(".jsonl.legacy"))
        stats["merged"] += 1
    return stats
