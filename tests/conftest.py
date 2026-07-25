"""v1.1.0 持仓生命周期建议层（B1）测试共享 fixture。

tempdir 隔离：monkeypatch apex.config.get 让 journal_dir / watchlist_file 指向 tmp_path，
patch 掉 _sector_for（tushare 行业查询，离线返回 None），fake_cfg 不含 push key -> 不发 HTTP。
"""
import pytest


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """所有存储路径指向 tmp_path，隔离 ~ 下真实数据。"""
    fake_cfg = {
        "paths": {
            "journal_dir": str(tmp_path),
            "watchlist_file": str(tmp_path / "watchlist.json"),
        }
    }
    monkeypatch.setattr("apex.config.get", lambda: fake_cfg)
    # _sector_for 调 tushare get_stock_info（离线/无 token 会失败），patch 掉避免网络
    monkeypatch.setattr("apex.watchlist._sector_for", lambda ts_code=None: None)
    return tmp_path
