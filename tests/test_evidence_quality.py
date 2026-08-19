from datetime import date

from apex.evidence import make_evidence_item, normalize_search_results, source_tier


def _result(title, url, snippet="", published="2026-08-17", site=""):
    return {
        "title": title,
        "url": url,
        "snippet": snippet,
        "date": published,
        "site": site,
    }


def test_filters_unrelated_money_flow_results_for_sanhua():
    rows = [
        _result(
            "8月17日天准科技(688003)龙虎榜数据",
            "https://stock.stockstar.com/a.html",
            "机构净买入6035万元",
        ),
        _result(
            "8月14日长盈通(688143)龙虎榜数据",
            "https://i.ifeng.com/b.html",
            "北向资金净买入4621万元",
        ),
        _result(
            "三花智控(002050.SZ)获机构净买入",
            "https://finance.eastmoney.com/c.html",
            "三花智控 002050 主力资金净流入",
        ),
    ]

    accepted, quality = normalize_search_results(
        rows, ts_code="002050.SZ", name="三花智控", category="money_flow",
        today=date(2026, 8, 19),
    )

    assert [row["title"] for row in accepted] == ["三花智控(002050.SZ)获机构净买入"]
    assert quality == {
        "raw_count": 3,
        "accepted_count": 1,
        "filtered_count": 2,
        "duplicate_count": 0,
        "tier_counts": {"1": 0, "2": 1, "3": 0},
    }


def test_filters_unrelated_regulatory_and_category_mismatch():
    rows = [
        _result("定安县综合行政执法局送达公告", "https://dingan.hainan.gov.cn/a"),
        _result(
            "三花智控最新行情走势",
            "https://quote.eastmoney.com/sz002050.html",
            "三花智控 002050 今日上涨",
        ),
        _result(
            "关于三花智控(002050)收到监管警示函的公告",
            "https://www.cninfo.com.cn/new/disclosure/detail?x=1",
            "证券代码002050，公司收到警示函",
        ),
    ]

    accepted, quality = normalize_search_results(
        rows, ts_code="002050.SZ", name="三花智控", category="regulatory",
        today=date(2026, 8, 19),
    )

    assert len(accepted) == 1
    assert accepted[0]["source_tier"] == 1
    assert quality["filtered_count"] == 2


def test_tier_comes_from_url_hostname_not_site_name():
    assert source_tier("https://www.cninfo.com.cn/a", "普通网页") == 1
    assert source_tier("https://finance.eastmoney.com/a", "未知站点") == 2
    assert source_tier("https://guba.eastmoney.com/news,1", "东方财富网") == 3


def test_results_sort_by_tier_then_newest_date():
    rows = [
        _result("三花智控002050收到监管函", "https://www.cninfo.com.cn/old", published="2026-08-10"),
        _result("三花智控002050收到处罚决定", "https://www.cninfo.com.cn/new", published="2026-08-18"),
        _result("三花智控002050监管新闻", "https://finance.eastmoney.com/a", published="2026-08-19"),
    ]

    accepted, _ = normalize_search_results(
        rows, ts_code="002050.SZ", name="三花智控", category="regulatory",
        today=date(2026, 8, 19),
    )

    assert [row["url"] for row in accepted] == [
        "https://www.cninfo.com.cn/new",
        "https://www.cninfo.com.cn/old",
        "https://finance.eastmoney.com/a",
    ]


def test_rejects_future_dates_and_deduplicates_reposts():
    rows = [
        _result(
            "三花智控002050收到监管函",
            "https://www.cninfo.com.cn/a?id=1&utm_source=x",
            "证券代码002050，收到监管函",
        ),
        _result(
            "三花智控002050收到监管函",
            "https://www.cninfo.com.cn/a?id=1&utm_source=y",
            "证券代码002050，收到监管函",
        ),
        _result(
            "三花智控002050未来公告",
            "https://www.cninfo.com.cn/b",
            "证券代码002050，收到监管函",
            published="2026-08-20",
        ),
    ]

    accepted, quality = normalize_search_results(
        rows, ts_code="002050.SZ", name="三花智控", category="regulatory",
        today=date(2026, 8, 19),
    )

    assert len(accepted) == 1
    assert quality["duplicate_count"] == 1
    assert quality["filtered_count"] == 1


def test_builds_structured_evidence_item():
    item = make_evidence_item(
        fact="公司于2026-08-18收到监管函",
        inference="合规风险上升",
        evidence_type="material_event",
        tool_name="web_search",
        source={
            "title": "三花智控公告",
            "url": "https://www.cninfo.com.cn/a",
            "date": "2026-08-18",
            "source_tier": 1,
            "entity_matched": True,
            "freshness_status": "current",
        },
    )

    assert item["id"].startswith("ev_")
    assert item["source_url"] == "https://www.cninfo.com.cn/a"
    assert item["source_tier"] == 1
    assert item["entity_matched"] is True
