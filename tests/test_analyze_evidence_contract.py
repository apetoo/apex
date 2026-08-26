import json
import inspect
from pathlib import Path

from apex import analyze, data


def test_record_verdict_requires_explicit_trade_intent_and_entry_semantics():
    tool = next(t for t in analyze.TOOLS if t["function"]["name"] == "record_verdict")
    params = tool["function"]["parameters"]

    assert params["properties"]["proposed_trade_action"]["enum"] == ["buy", "watch", "avoid"]
    assert params["properties"]["entry_style"]["enum"] == ["pullback", "breakout"]
    assert "valid_for_days" in params["properties"]
    assert {"proposed_trade_action", "entry_style", "valid_for_days"} <= set(params["required"])


def test_analyze_run_accepts_candidate_context():
    assert "candidate_context" in inspect.signature(analyze.run).parameters


def test_research_state_tool_replaces_mandatory_search_categories():
    names = {tool["function"]["name"] for tool in analyze.TOOLS}

    assert "submit_research_state" in names
    assert not hasattr(data, "MANDATORY_SEARCH_CATEGORIES")


def test_analysis_prompt_describes_gap_driven_search_not_four_required_calls():
    source = Path(analyze.__file__).read_text(encoding="utf-8")
    persona = Path("apex/prompts/expert-persona.md").read_text(encoding="utf-8")

    assert "博查 4 类强制" not in source
    assert "完成强制博查类别" not in persona
    assert "按证据缺口" in source


def test_position_trim_does_not_require_search_call_checklist(isolated_paths):
    tool_input = {"action": "trim", "trim_pct": 0.25, "rationale": "跌破关键位"}

    reasons, _ = analyze._validate_position_action(
        tool_input, "002050.SZ", searches_performed=[], current_price=40.0,
    )

    assert not any("web_search" in reason or "regulatory" in reason for reason in reasons)


def test_safety_scan_outcome_is_execution_not_tier_one_hit():
    # 601872 根因：扫描只命中 Tier 2 被误判"失败"，事件还被整条丢弃
    assert analyze._safety_scan_outcome({"results": [{"source_tier": 2}]}) is True
    assert analyze._safety_scan_outcome({"results": []}) is True
    assert analyze._safety_scan_outcome({"error": "HTTP 500"}) is False


def test_tool_evidence_classifies_web_rows_by_content_not_query_category():
    raw = json.dumps({
        "category": "general",
        "results": [
            {
                "title": "招商轮船:收到上海监管局《行政监管措施决定书》",
                "snippet": "监管警示",
                "url": "https://www.stcn.com/article/detail/3916366.html",
                "date": "2026-05-19",
                "site": "证券时报",
                "source_tier": 2,
                "entity_matched": True,
                "freshness_status": "current",
            },
        ],
    }, ensure_ascii=False)

    items = analyze._tool_evidence("web_search", raw, "601872.SH")

    assert [item["evidence_type"] for item in items] == ["regulatory"]


def test_tool_evidence_classifies_mx_announcement_as_earnings_tier_one(monkeypatch):
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"601872.SH": "招商轮船"})
    raw = json.dumps({
        "source": "mx:news",
        "query": "招商轮船 2026 半年报 业绩预告",
        "count": 1,
        "results": [{
            "title": "招商轮船:招商轮船2026年半年度业绩预增公告",
            "content": "预计净利润66亿元至73亿元，同比增长214%至248%",
            "institution": "巨潮资讯", "type": "", "date": "2026-07-06",
        }],
    }, ensure_ascii=False)

    items = analyze._tool_evidence("mx_news_search", raw, "601872.SH")

    assert len(items) == 1
    assert items[0]["evidence_type"] == "earnings"
    assert items[0]["source_tier"] == 1
    assert "66亿元至73亿元" in items[0]["fact"]


def test_tool_evidence_does_not_promote_unattributed_mx_commentary_to_tier_one(monkeypatch):
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000725.SZ": "京东方A"})
    raw = json.dumps({
        "source": "mx:news",
        "query": "京东方A 最新消息",
        "count": 1,
        "results": [{
            "title": "科技龙头错杀？京东方A的世界之最",
            "content": "将军夜引弓认为面板周期已经反转",
            "type": "", "date": "2026-08-24",
        }],
    }, ensure_ascii=False)

    items = analyze._tool_evidence("mx_news_search", raw, "000725.SZ")

    assert len(items) == 1
    assert items[0]["source_tier"] == 3


def test_tool_evidence_does_not_treat_earnings_preview_commentary_as_disclosure(monkeypatch):
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000725.SZ": "京东方A"})
    raw = json.dumps({
        "source": "mx:news", "count": 1,
        "results": [{
            "title": "京东方A业绩预告解读：周期反转在即？",
            "content": "财富号作者个人观点", "type": "", "date": "2026-08-24",
        }],
    }, ensure_ascii=False)

    items = analyze._tool_evidence("mx_news_search", raw, "000725.SZ")

    assert items[0]["source_tier"] == 3


def test_tool_evidence_classifies_recognized_mx_financial_media_as_tier_two(monkeypatch):
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000725.SZ": "京东方A"})
    raw = json.dumps({
        "source": "mx:news",
        "query": "京东方A 行业动态",
        "count": 1,
        "results": [{
            "title": "京东方A：面板价格环比变化",
            "content": "证券时报报道了最新面板行业数据",
            "institution": "证券时报", "type": "新闻", "date": "2026-08-24",
        }],
    }, ensure_ascii=False)

    items = analyze._tool_evidence("mx_news_search", raw, "000725.SZ")

    assert len(items) == 1
    assert items[0]["source_tier"] == 2


def test_mx_news_filters_official_announcement_for_another_company(monkeypatch):
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    raw = json.dumps({
        "source": "mx:news", "count": 1,
        "results": [{
            "title": "工业富联:2026年半年度报告",
            "content": "工业富联上半年营业收入和净利润增长",
            "entity": "工业富联(601138.SH)", "institution": "巨潮资讯",
            "type": "", "date": "2026-08-20",
        }],
    }, ensure_ascii=False)

    assert analyze._tool_evidence("mx_news_search", raw, "000977.SZ") == []


def test_unattributed_mx_announcement_title_stays_tier_three(monkeypatch):
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    raw = json.dumps({
        "source": "mx:news", "count": 1,
        "results": [{
            "title": "浪潮信息重大利好公告", "content": "作者称将登陆交易所官网核实",
            "institution": "", "type": "", "date": "2026-08-24",
        }],
    }, ensure_ascii=False)

    items = analyze._tool_evidence("mx_news_search", raw, "000977.SZ")

    assert len(items) == 1
    assert items[0]["source_tier"] == 3


def test_mx_official_url_is_tier_one(monkeypatch):
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    raw = json.dumps({
        "source": "mx:news", "count": 1,
        "results": [{
            "title": "浪潮信息:2026年半年度报告", "content": "公司披露半年度数据",
            "url": "https://www.cninfo.com.cn/new/disclosure/detail/000977",
            "institution": "", "type": "", "date": "2026-08-24",
        }],
    }, ensure_ascii=False)

    items = analyze._tool_evidence("mx_news_search", raw, "000977.SZ")

    assert items[0]["source_tier"] == 1


def test_mx_investor_qa_shareholder_count_is_not_material_regulatory_evidence(monkeypatch):
    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    raw = json.dumps({
        "source": "mx:news", "count": 1,
        "results": [{
            "title": "浪潮信息：截至2026年8月10日公司股东总户数为26万余户",
            "content": "公司在互动平台回答投资者提问。页面还包含其他公司募集资金监管新闻链接。",
            "institution": "证券日报", "type": "新闻", "date": "2026-08-19",
        }],
    }, ensure_ascii=False)

    items = analyze._tool_evidence("mx_news_search", raw, "000977.SZ")

    assert items[0]["evidence_type"] == "general"


def test_tool_evidence_keeps_plain_price_json_as_structured_data():
    raw = json.dumps(
        [{"trade_date": "20260819", "close": 18.6, "ma5": 17.73, "ma20": 17.07,
          "ma60": 16.42, "vol_ratio": 1.78}],
        ensure_ascii=False,
    )

    items = analyze._tool_evidence("get_daily_price", raw, "601872.SH")

    assert [item["evidence_type"] for item in items] == ["structured_data"]


def test_mx_data_query_ledger_fact_carries_values_not_row_counts():
    # 002594/601872 复盘：旧摘要器只留 entity+行数，独立复核对着 ledger 找不到
    # "66-73亿(+214%~248%)"这类关键数字，判"无具体数值"强制 rework -> 错误弃权
    raw = json.dumps({
        "query": "招商轮船 2026年上半年 净利润 业绩预告",
        "tables": [{
            "entity": "招商轮船(601872.SH)",
            "rows": [{
                "date": "2026中报", "业绩预告类型": "预增",
                "业绩预告摘要": "预计2026年1-6月净利润盈利:660,000万元至730,000万元",
                "业绩预告变动原因": "国际油轮运输市场进入超级景气周期……（长文本）",
                "预告归属于母公司的净利润上限": "73亿元",
                "预告归属于母公司的净利润下限": "66亿元",
            }],
        }],
    }, ensure_ascii=False)

    items = analyze._tool_evidence("mx_data_query", raw, "601872.SH")

    assert len(items) == 1
    fact = items[0]["fact"]
    assert "上限=73亿元" in fact
    assert "下限=66亿元" in fact
    assert "油轮运输市场" not in fact  # 摘要/原因等长文本不进 ledger


def test_web_evidence_fact_includes_snippet_values():
    raw = json.dumps({
        "category": "research",
        "results": [{
            "title": "国泰海通：招商轮船深度报告",
            "snippet": "给予买入评级，目标价26.9元（2026年15倍PE）",
            "url": "https://www.stcn.com/article/detail/1.html",
            "date": "2026-08-10", "site": "证券时报",
            "source_tier": 2, "entity_matched": True, "freshness_status": "current",
        }],
    }, ensure_ascii=False)

    items = analyze._tool_evidence("web_search", raw, "601872.SH")

    assert len(items) == 1
    assert "目标价26.9元" in items[0]["fact"]


def test_review_sees_system_context_and_materiality_guidance():
    source = Path(analyze.__file__).read_text(encoding="utf-8")

    # reviewer 必须能看到系统注入上下文（大盘/情绪/持仓计划），否则候选引用
    # 这些数据会被判"未经验证的假设"，且 agent 无法靠补搜修复
    assert '"system_context": system_context' in source
    assert "system_context 是系统注入的确定性数据" in source
    # run() 把注入 blocks 传进 loop
    assert "system_context=" in source
    assert "history_block, portfolio_block, intraday_block, market_block, playstyle_block" in source
    # 次要技术出入不应单独触发 rework（002594 "MA10未提供"式 nitpick）
    assert "不应单独导致 rework" in source
