from click.testing import CliRunner

import main
from apex import analyze as analyze_module


def test_analyze_cli_displays_insufficient_evidence_without_direction(monkeypatch):
    def fake_run(*_args, **kwargs):
        kwargs["on_progress"]({
            "type": "status", "stage": "researching",
            "message": "正在补证", "current": 2, "total": 3,
        })
        return {
            "analysis_status": "insufficient_evidence",
            "verdict": None,
            "confidence": None,
            "unknowns": ["重大公告未能从权威来源核实"],
            "research_summary": "补证预算内仍有关键未知。",
            "outcome_reason": "evidence_gap",
            "next_actions": ["核实交易所最新公告"],
            "evidence": [{"fact": "已取得最新日线行情"}],
            "analysis_text": "",
        }
    monkeypatch.setattr(analyze_module, "run", fake_run)

    result = CliRunner().invoke(main.analyze, ["002050.SZ", "--no-save"])

    assert result.exit_code == 0
    assert "证据不足，暂不判断" in result.output
    assert "重大公告未能从权威来源核实" in result.output
    assert "正在补证 (2/3)" in result.output
    assert "关键证据尚未核实" in result.output
    assert "已确认事实: 1 条" in result.output
    assert "核实交易所最新公告" in result.output
    assert "置信度" not in result.output
    assert "已保存" not in result.output
