"""24h 重复分析限幅的单测（痛点#3透明化）。

测纯函数 _compute_repeat_analysis / _apply_repeat_limit / _clamp_verdict_delta，
不依赖文件系统、不调 DeepSeek API。
"""
from datetime import datetime, timedelta, timezone

from apex.analyze import (
    _apply_repeat_limit,
    _clamp_verdict_delta,
    _compute_repeat_analysis,
    _finalize_verdict_candidate,
    _audit_repeat_candidate,
)
from apex.schemas import VERDICT_ENUM

_TZ = timezone(timedelta(hours=8))


def _entry(verdict="偏多", confidence=7, hours_ago=3, features=None):
    """构造一条历史 journal entry，analyzed_at 距今 hours_ago 小时。"""
    at = (datetime.now(_TZ) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return {
        "ts_code": "002050.SZ",
        "verdict": verdict,
        "confidence": confidence,
        "analyzed_at": at,
        "features": features or _feat(),
    }


def _feat(price_vs_ma5=0.0, rsi=50.0, volume_ratio=1.0):
    return {
        "price_vs_ma5_pct": price_vs_ma5,
        "rsi_14": rsi,
        "volume_ratio": volume_ratio,
    }


# ── _compute_repeat_analysis ────────────────────────────────────────────

def test_first_analysis_no_history():
    """首次分析（无历史）-> is_repeat=False，不限幅。"""
    ra = _compute_repeat_analysis([], "看多", 8, [], _feat())
    assert ra["is_repeat"] is False
    assert ra["new_info_verified"] is False


def test_repeat_beyond_24h_not_flagged():
    """>24h 重复 -> is_repeat=False。"""
    history = [_entry(hours_ago=25)]
    ra = _compute_repeat_analysis(history, "看多", 8, [], _feat())
    assert ra["is_repeat"] is False


def test_repeat_within_24h_no_new_info():
    """24h 内重复 + 无 new_info -> is_repeat=True, new_info_verified=False。"""
    history = [_entry(verdict="偏多", confidence=7, hours_ago=3, features=_feat())]
    ra = _compute_repeat_analysis(history, "看多", 8, [], _feat())
    assert ra["is_repeat"] is True
    assert ra["new_info_verified"] is False
    assert ra["last_verdict"] == "偏多"
    assert ra["last_confidence"] == 7
    assert ra["hours_since_last"] < 24


def test_repeat_with_new_info_and_feature_change_verified():
    """24h 内重复 + new_info + 技术面变化大 -> new_info_verified=True。"""
    last_feat = _feat(price_vs_ma5=0.0, rsi=50.0, volume_ratio=1.0)
    cur_feat = _feat(price_vs_ma5=5.0, rsi=65.0, volume_ratio=1.5)  # 三项都超阈值
    history = [_entry(verdict="偏多", confidence=7, hours_ago=3, features=last_feat)]
    ra = _compute_repeat_analysis(history, "看多", 8, ["放量突破 MA20"], cur_feat)
    assert ra["is_repeat"] is True
    assert ra["new_info_verified"] is True


def test_repeat_with_new_info_but_no_feature_change_rejected():
    """24h 内重复 + new_info + 技术面没变 -> new_info_verified=False（宁可错杀）。"""
    last_feat = _feat(price_vs_ma5=1.0, rsi=50.0, volume_ratio=1.0)
    cur_feat = _feat(price_vs_ma5=1.5, rsi=52.0, volume_ratio=1.1)  # 全部低于阈值
    history = [_entry(verdict="偏多", confidence=7, hours_ago=3, features=last_feat)]
    ra = _compute_repeat_analysis(history, "看多", 8, ["基本面改善"], cur_feat)
    assert ra["is_repeat"] is True
    assert ra["new_info_verified"] is False  # 客观没变，驳回 AI 声称


def test_delta_calculation():
    """verdict_delta / confidence_delta 计算正确。"""
    # VERDICT_ENUM: 看多0 偏多1 观望偏多2 中性3 观望偏空4 偏空5 看空6
    history = [_entry(verdict="偏多", confidence=7, hours_ago=3, features=_feat())]
    ra = _compute_repeat_analysis(history, "看空", 8, [], _feat())
    assert ra["verdict_delta"] == 5   # 6 - 1，向空
    assert ra["confidence_delta"] == 1  # 8 - 7


# ── _apply_repeat_limit ─────────────────────────────────────────────────

def test_limit_verdict_delta_clamped():
    """24h 重复 + 无 new_info + 方向变化≥2档 -> verdict clamp 到 ±1 档。"""
    # 偏多(idx1) -> 看空(idx6), delta=+5, clamp 到 idx2=观望偏多
    history = [_entry(verdict="偏多", confidence=7, hours_ago=3, features=_feat())]
    ra = _compute_repeat_analysis(history, "看空", 8, [], _feat())
    limited_v, limited_c = _apply_repeat_limit(ra, "看空", 8)
    assert limited_v == "观望偏多"
    assert limited_c == 8  # conf delta=1, 不 clamp
    assert ra["limited"] is True
    assert "verdict_delta_clamped" in ra["limit_rule"]


def test_limit_conf_clamped():
    """24h 重复 + 无 new_info + conf 变化>2 -> conf clamp 到 ±2。"""
    history = [_entry(verdict="看多", confidence=7, hours_ago=3, features=_feat())]
    ra = _compute_repeat_analysis(history, "看多", 3, [], _feat())  # conf 7->3, delta=-4
    limited_v, limited_c = _apply_repeat_limit(ra, "看多", 3)
    assert limited_v == "看多"  # verdict 没变
    assert limited_c == 5  # 7 + max(-2, min(2, -4)) = 7-2 = 5
    assert ra["limited"] is True
    assert "conf_clamped" in ra["limit_rule"]


def test_finalize_candidate_applies_repeat_limit_before_report_generation():
    history = [_entry(verdict="偏多", confidence=7, hours_ago=3, features=_feat())]
    raw = {
        "verdict": "看空", "confidence": 3, "new_info": [], "features": _feat(),
        "evidence": ["趋势走弱 → 偏空"],
    }

    final, metadata = _finalize_verdict_candidate(raw, history)

    assert final["verdict"] == "观望偏多"
    assert final["confidence"] == 5
    assert raw["verdict"] == "看空"
    assert metadata["repeat_analysis"]["raw_verdict"] == "看空"
    assert metadata["repeat_analysis"]["raw_confidence"] == 3


def test_policy_repeat_audit_never_mutates_authoritative_trade_state():
    history = [_entry(verdict="看多", confidence=3, hours_ago=2, features=_feat())]
    raw = {
        "verdict": "偏空", "confidence": 8, "new_info": [], "features": _feat(),
        "proposed_trade_action": "avoid", "position_size_pct": 0,
        "entry": 0, "stop_loss": 0, "target": 0,
    }

    final, metadata = _audit_repeat_candidate(raw, history)

    assert final == raw
    assert metadata["repeat_analysis"]["legacy_suggested_verdict"] != raw["verdict"]
    assert metadata["repeat_analysis"]["limited"] is False


def test_no_limit_when_new_info_verified():
    """24h 重复 + new_info_verified=True -> 不限幅。"""
    last_feat = _feat(price_vs_ma5=0.0, rsi=50.0, volume_ratio=1.0)
    cur_feat = _feat(price_vs_ma5=5.0, rsi=65.0, volume_ratio=1.5)
    history = [_entry(verdict="偏多", confidence=7, hours_ago=3, features=last_feat)]
    ra = _compute_repeat_analysis(history, "看空", 8, ["新增：放量突破"], cur_feat)
    assert ra["new_info_verified"] is True
    limited_v, limited_c = _apply_repeat_limit(ra, "看空", 8)
    assert limited_v == "看空"
    assert limited_c == 8
    assert ra["limited"] is False


def test_no_limit_beyond_24h():
    """>24h -> is_repeat=False -> 不限幅。"""
    history = [_entry(verdict="偏多", confidence=7, hours_ago=25, features=_feat())]
    ra = _compute_repeat_analysis(history, "看空", 8, [], _feat())
    limited_v, limited_c = _apply_repeat_limit(ra, "看空", 8)
    assert limited_v == "看空"
    assert ra["limited"] is False


def test_no_limit_first_analysis():
    """首次分析 -> 不限幅。"""
    ra = _compute_repeat_analysis([], "看多", 8, [], _feat())
    limited_v, limited_c = _apply_repeat_limit(ra, "看多", 8)
    assert limited_v == "看多"
    assert ra["limited"] is False


# ── _clamp_verdict_delta ────────────────────────────────────────────────

def test_clamp_verdict_delta_unit():
    # VERDICT_ENUM: 看多0 偏多1 观望偏多2 中性3 观望偏空4 偏空5 看空6
    assert _clamp_verdict_delta("偏多", "看空", 1) == "观望偏多"   # +5 -> +1
    assert _clamp_verdict_delta("看空", "看多", 1) == "偏空"       # -6 -> -1
    assert _clamp_verdict_delta("偏多", "中性", 1) == "观望偏多"   # +2 -> +1
    assert _clamp_verdict_delta("偏多", "观望偏多", 1) == "观望偏多"  # +1, 不 clamp
    assert _clamp_verdict_delta("偏多", "偏多", 1) == "偏多"       # 0, 不 clamp
    # last 不在 ENUM -> 原样返回 raw
    assert _clamp_verdict_delta("未知方向", "看多", 1) == "看多"


def test_verdict_enum_ordering_unchanged():
    """档位顺序是限幅逻辑的地基，锁住防误改。"""
    assert VERDICT_ENUM == ["看多", "偏多", "观望偏多", "中性", "观望偏空", "偏空", "看空"]
