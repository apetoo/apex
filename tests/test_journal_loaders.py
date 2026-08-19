"""v1.1.0 B1 T2: journal loader 拆分（load_verdicts / load_position_actions / load_latest_verdict）。

CRITICAL 回归：position_action 记录（source=position_action, verdict=None）不得泄漏进
load_verdicts -- 方向 call 语义消费点（backtest/calibration/repeat-analysis/下单反查/
ATR 估算/候选同步/postmortem/system）全用 load_verdicts，position_action 混入会让
None verdict 静默断（不命中 BULLISH、_verdict_index 返回 -1、price_advice 缺失）。
历史展示（/journal、/analyze 历史列表）用 load_entries（含 position_action），不用 load_verdicts。
"""
from apex import journal
from apex.schemas import POSITION_ACTION_SOURCE


def _write_verdict(ts_code, analyzed_at, verdict="看多"):
    journal.write_entry({
        "ts_code": ts_code, "name": "test",
        "date": analyzed_at[:10], "analyzed_at": analyzed_at,
        "verdict": verdict, "confidence": 7,
        "price_advice": {"entry": 12.0, "stop_loss": 11.0, "target": 14.0},
        "features": {"ma5_position": "above", "ma20_position": "above",
                     "volume_ratio": 1.2, "rsi_14": 55, "atr_14_pct": 3.0},
        "evidence": ["x"], "analysis_text": "", "source": "standalone",
    })


def _write_pa(ts_code, analyzed_at, action="hold"):
    journal.write_entry({
        "ts_code": ts_code, "name": "test",
        "date": analyzed_at[:10], "analyzed_at": analyzed_at,
        "source": POSITION_ACTION_SOURCE,
        "position_action": {"action": action, "rationale": "test", "scale_plan": []},
        "analysis_text": "", "verdict": None, "confidence": None,
        "price_advice": None, "features": None, "evidence": None,
    })


# ── 基础拆分 ─────────────────────────────────────────────────────────────────

def test_load_verdicts_excludes_position_actions(isolated_paths):
    """verdict + position_action 同文件 -> load_verdicts 只返回 verdict。"""
    _write_verdict("002050.SZ", "2026-07-01T10:00:00+08:00")
    _write_pa("002050.SZ", "2026-07-02T10:00:00+08:00", action="add")
    vs = journal.load_verdicts("002050.SZ")
    assert len(vs) == 1
    assert vs[0]["verdict"] == "看多"
    assert vs[0]["source"] != POSITION_ACTION_SOURCE


def test_load_position_actions_excludes_verdicts(isolated_paths):
    _write_verdict("002050.SZ", "2026-07-01T10:00:00+08:00")
    _write_pa("002050.SZ", "2026-07-02T10:00:00+08:00", action="trim")
    pas = journal.load_position_actions("002050.SZ")
    assert len(pas) == 1
    assert pas[0]["source"] == POSITION_ACTION_SOURCE
    assert pas[0]["position_action"]["action"] == "trim"


def test_load_entries_still_returns_all(isolated_paths):
    """load_entries 不变，返回全量（MCP journal read / system 计数用）。"""
    _write_verdict("002050.SZ", "2026-07-01T10:00:00+08:00")
    _write_pa("002050.SZ", "2026-07-02T10:00:00+08:00")
    all_e = journal.load_entries("002050.SZ")
    assert len(all_e) == 2


# ── load_latest_verdict ──────────────────────────────────────────────────────

def test_load_latest_verdict_ignores_newer_position_action(isolated_paths):
    """position_action 更新，但 load_latest_verdict 仍返回最近 verdict（方向 view）。"""
    _write_verdict("002050.SZ", "2026-07-01T10:00:00+08:00", verdict="偏多")
    _write_pa("002050.SZ", "2026-07-05T10:00:00+08:00")  # 更新
    latest_v = journal.load_latest_verdict("002050.SZ")
    assert latest_v is not None
    assert latest_v["verdict"] == "偏多"
    assert latest_v["source"] != POSITION_ACTION_SOURCE


def test_load_latest_verdict_none_when_only_position_actions(isolated_paths):
    """只有 position_action（无 verdict）-> load_latest_verdict None，不误返 position_action。"""
    _write_pa("002050.SZ", "2026-07-01T10:00:00+08:00")
    assert journal.load_latest_verdict("002050.SZ") is None


def test_load_latest_verdict_empty(isolated_paths):
    assert journal.load_latest_verdict("002050.SZ") is None


# ── CRITICAL 回归：position_action 不污染方向 call 消费点 ──────────────────────

def test_critical_position_action_not_in_load_verdicts(isolated_paths):
    """CRITICAL: position_action 记录 verdict=None，绝不能进 load_verdicts。

    模拟 9 消费点：backtest._is_bullish(None) 跳过、_verdict_index('')=-1、
    _journal_ref_for 取 verdict=None 会断 -- 全靠 load_verdicts 在源头过滤。
    """
    _write_pa("002050.SZ", "2026-07-01T10:00:00+08:00", action="add")
    _write_pa("002050.SZ", "2026-07-02T10:00:00+08:00", action="trim")
    _write_verdict("002050.SZ", "2026-07-03T10:00:00+08:00", verdict="看多")
    _write_pa("002050.SZ", "2026-07-04T10:00:00+08:00", action="exit")

    vs = journal.load_verdicts("002050.SZ")
    # 3 条 position_action 全部被过滤，只剩 1 条 verdict
    assert len(vs) == 1
    assert all(v.get("verdict") is not None for v in vs)
    assert all(v["source"] != POSITION_ACTION_SOURCE for v in vs)

    pas = journal.load_position_actions("002050.SZ")
    assert len(pas) == 3
    assert all(p["source"] == POSITION_ACTION_SOURCE for p in pas)


def test_critical_empty_verdicts_edge(isolated_paths):
    """edge: 只有 position_action 时，load_verdicts 返回 []（非 None、不抛）。"""
    _write_pa("002050.SZ", "2026-07-01T10:00:00+08:00")
    assert journal.load_verdicts("002050.SZ") == []
    assert journal.load_verdicts("999999.SZ") == []  # 完全无文件


def test_insufficient_evidence_never_enters_verdict_or_position_action_loaders(isolated_paths):
    journal.write_entry({
        "ts_code": "002050.SZ", "name": "三花智控",
        "date": "2026-08-19", "analyzed_at": "2026-08-19T10:00:00+08:00",
        "analysis_status": "insufficient_evidence", "source": "standalone",
        "verdict": None, "confidence": None, "price_advice": None,
        "position_action": None, "features": None, "evidence": [],
        "unknowns": ["重大事件无法核实"], "research_summary": "暂不判断",
    })

    assert journal.load_verdicts("002050.SZ") == []
    assert journal.load_position_actions("002050.SZ") == []
    assert journal.load_entries("002050.SZ")[0]["analysis_status"] == "insufficient_evidence"


def test_legacy_verdict_defaults_to_completed_status(isolated_paths):
    _write_verdict("002050.SZ", "2026-07-01T10:00:00+08:00")
    assert journal.load_entries("002050.SZ")[0]["analysis_status"] == "completed"


# ── 历史展示端点含 position_action（B1 回归） ──────────────────────────────────

def test_list_journal_endpoint_includes_position_action(isolated_paths):
    """历史展示端点（/api/journal, /api/journal/{ts_code}）必须含 position_action。

    B1 回归：loader 拆分时曾误把展示端点迁到 load_verdicts，导致持仓分析在历史页看不到。
    展示用 load_entries（全量），语义消费才用 load_verdicts。
    """
    from unittest.mock import patch
    from backend.routers.analyze import list_journal, list_all_journal

    with patch("apex.data.get_name_map", return_value={}):
        _write_verdict("002050.SZ", "2026-07-01T10:00:00+08:00")
        _write_pa("002050.SZ", "2026-07-02T10:00:00+08:00", action="add")

        per_stock = list_journal("002050.SZ")
        assert len(per_stock) == 2  # verdict + position_action
        assert any(e.get("source") == POSITION_ACTION_SOURCE for e in per_stock)

        all_entries = list_all_journal()
        assert any(e.get("source") == POSITION_ACTION_SOURCE for e in all_entries)


def test_latest_journal_endpoint_returns_position_action_when_newest(isolated_paths):
    """latest_journal 返回最近一条 entry（含 position_action），供'同步AI'按钮取 new_stop。

    曾用 load_latest_verdict（排除 position_action）导致持仓后同步按钮取到旧 verdict 的 stop，
    与 position_action 自动覆盖的 new_stop 冲突（点同步会回退）。改 load_latest 修复。
    """
    from unittest.mock import patch
    from backend.routers.analyze import latest_journal

    with patch("apex.data.get_name_map", return_value={}):
        _write_verdict("002050.SZ", "2026-07-01T10:00:00+08:00")
        _write_pa("002050.SZ", "2026-07-05T10:00:00+08:00", action="hold")
        latest = latest_journal("002050.SZ")
        assert latest is not None
        assert latest["source"] == POSITION_ACTION_SOURCE
        assert latest["position_action"]["action"] == "hold"
