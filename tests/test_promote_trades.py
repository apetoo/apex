"""方案0 成交录入断层修复的 smoke test。

覆盖：
- 断层1: promote 写 trades.jsonl（对齐 buy() 流水口径）
- 断层2: buy 自动归档同 code 候选（状态机闭合）
- 断层3: promote 缺 shares 时 ATR 推算 + size_method 标注；无 journal 退化
- 配套:   account missing_warning 透明化
- 断层4: monitor._emit_trigger 把候选 stop_advice/target_advice 带进 trigger record

tempdir 隔离：monkeypatch apex.config.get 让所有路径指向 tmp_path，
fake_cfg 不含 push key → push.enabled=False → 不发 HTTP。
"""
import pytest

from apex import watchlist, trades, account, journal


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """所有存储路径指向 tmp_path，隔离 ~ 下真实数据。"""
    fake_cfg = {
        "paths": {
            "journal_dir": str(tmp_path),
            "watchlist_file": str(tmp_path / "watchlist.json"),
        }
        # 故意不含 push key → push._enabled() 返回 False，不发 HTTP
    }
    monkeypatch.setattr("apex.config.get", lambda: fake_cfg)
    return tmp_path


def _add_candidate(ts_code="002050.SZ", name="test", trigger_price=12.0):
    # 传 expires_days 跳过 _dynamic_expiry_days（它会调 tushare API）
    watchlist.add_candidate(ts_code, name, trigger_price,
                           trigger_direction="above", expires_days=7)


def _write_journal_with_atr(ts_code="002050.SZ", atr_pct=3.0):
    """造一条带 atr_14_pct 的 journal entry，供 ATR 推算读取。"""
    journal.write_entry({
        "ts_code": ts_code, "name": "test",
        "date": "2026-07-07", "analyzed_at": "2026-07-07T10:00:00+08:00",
        "verdict": "看多", "confidence": 7,
        "price_advice": {"entry": 12.0, "stop_loss": 11.0, "target": 14.0},
        "features": {"atr_14_pct": atr_pct, "ma5_position": "above",
                     "ma20_position": "above", "volume_ratio": 1.2, "rsi_14": 55},
        "evidence": ["x"], "analysis_text": "",
    })


# ── 断层1: promote 写 trades ──────────────────────────────────────────────────

def test_promote_writes_trade(isolated_paths):
    """promote 必须写一条 buy trade 到 trades.jsonl（对齐 buy() 流水口径）。"""
    _add_candidate()
    record = watchlist.promote_candidate(
        "002050.SZ", entry_price=12.5, stop_loss=11.5, target=14.0,
        position_size_shares=1000,
    )
    assert record["position_size_shares"] == 1000
    # 用户手填 shares → 不标 size_method
    assert "size_method" not in record

    tl = trades.load_trades()
    assert len(tl) == 1
    assert tl[0]["side"] == "buy"
    assert tl[0]["shares"] == 1000
    assert tl[0]["fill_price"] == 12.5
    assert tl[0]["note"] == "promoted from candidate"

    # 候选已归档为 promoted
    wl = watchlist.load()
    assert not wl["candidates"]
    assert len(wl["active_positions"]) == 1


# ── 断层2: buy 自动归档候选 ───────────────────────────────────────────────────

def test_buy_archives_candidate(isolated_paths):
    """绕过 promote 直接 buy 同 code 候选 → 候选归档 bought_directly。"""
    _add_candidate()
    watchlist.buy("002050.SZ", fill_price=12.5, shares=1000)

    wl = watchlist.load()
    assert not wl["candidates"]  # 候选被归档，不再重复触发
    archived = [a for a in wl["archived"] if a.get("ts_code") == "002050.SZ"]
    assert archived
    assert archived[0]["status"] == "archived_bought_directly"
    assert len(wl["active_positions"]) == 1


def test_buy_no_candidate_no_archive(isolated_paths):
    """无候选时 buy 不动 archived。"""
    watchlist.buy("002050.SZ", fill_price=12.5, shares=1000)
    wl = watchlist.load()
    assert not wl["archived"]
    assert len(wl["active_positions"]) == 1


# ── 断层3: promote 缺 shares 时 ATR 推算 ──────────────────────────────────────

def test_promote_estimates_shares_from_journal(isolated_paths):
    """未传 shares + 有 journal ATR → 推算手数 + size_method=atr_estimated + 写 trades。"""
    _add_candidate()
    _write_journal_with_atr(atr_pct=3.0)

    record = watchlist.promote_candidate(
        "002050.SZ", entry_price=12.5, stop_loss=11.5, target=14.0,
        # 不传 position_size_shares
    )
    assert record["size_method"] == "atr_estimated"
    assert record["position_size_shares"] > 0
    # 推算成功也写 trades
    tl = trades.load_trades()
    assert len(tl) == 1
    assert tl[0]["shares"] == record["position_size_shares"]


def test_promote_missing_shares_no_journal(isolated_paths):
    """未传 shares + 无 journal → size_method=missing，不写 trades（0 股污染统计）。"""
    _add_candidate()
    record = watchlist.promote_candidate(
        "002050.SZ", entry_price=12.5, stop_loss=11.5, target=14.0,
    )
    assert record["size_method"] == "missing"
    assert not record.get("position_size_shares")
    assert trades.load_trades() == []


# ── 配套: account missing_warning ─────────────────────────────────────────────

def test_account_missing_warning(isolated_paths):
    """持仓缺手数时 current_total_risk 返回 missing_warning 文案。"""
    # 直接 add_position 不传 shares（绕过 promote 的推算兜底）
    watchlist.add_position("002050.SZ", "test", entry_price=12.5,
                           stop_loss=11.5, target=14.0)
    wl = watchlist.load()
    risk = account.current_total_risk(wl["active_positions"])
    assert risk["missing_size_count"] == 1
    assert risk["missing_warning"] is not None
    assert "1 笔" in risk["missing_warning"]


def test_account_no_warning_when_complete(isolated_paths):
    """持仓数据完整时 missing_warning=None。"""
    watchlist.add_position("002050.SZ", "test", entry_price=12.5,
                           stop_loss=11.5, target=14.0,
                           position_size_shares=1000)
    wl = watchlist.load()
    risk = account.current_total_risk(wl["active_positions"])
    assert risk["missing_size_count"] == 0
    assert risk["missing_warning"] is None


# ── 断层4: monitor._emit_trigger 带上下文 ─────────────────────────────────────

def test_monitor_emit_trigger_carries_candidate_advice():
    """候选触发 record 带上 stop_advice/target_advice/note。"""
    from apex import monitor
    cand = {
        "ts_code": "002050.SZ", "name": "test",
        "trigger_price": 12.0, "stop_advice": 11.0, "target_advice": 14.0,
        "note": "test note",
    }
    rec = monitor._emit_trigger(
        cand, "002050.SZ", "test", "candidates", 12.5,
        {"type": "candidate", "trigger_price": 12.0},
    )
    assert rec["stop_advice"] == 11.0
    assert rec["target_advice"] == 14.0
    assert rec["note"] == "test note"


def test_monitor_emit_trigger_carries_position_advice():
    """持仓触发 record 带上 stop_loss/target/entry_price。"""
    from apex import monitor
    pos = {
        "ts_code": "002050.SZ", "name": "test",
        "entry_price": 12.0, "stop_loss": 11.0, "target": 14.0,
    }
    rec = monitor._emit_trigger(
        pos, "002050.SZ", "test", "active_positions", 10.9,
        {"type": "stop_loss", "trigger_price": 11.0},
    )
    assert rec["stop_loss"] == 11.0
    assert rec["target"] == 14.0
    assert rec["entry_price"] == 12.0
