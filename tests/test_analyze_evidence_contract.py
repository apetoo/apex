from pathlib import Path

from apex import analyze, data


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
