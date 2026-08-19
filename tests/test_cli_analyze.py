from click.testing import CliRunner

import main
from apex import analyze as analyze_module


def test_analyze_cli_displays_insufficient_evidence_without_direction(monkeypatch):
    monkeypatch.setattr(
        analyze_module,
        "run",
        lambda *_args, **_kwargs: {
            "analysis_status": "insufficient_evidence",
            "verdict": None,
            "confidence": None,
            "unknowns": ["重大公告未能从权威来源核实"],
            "research_summary": "补证预算内仍有关键未知。",
            "analysis_text": "",
        },
    )

    result = CliRunner().invoke(main.analyze, ["002050.SZ", "--no-save"])

    assert result.exit_code == 0
    assert "证据不足，暂不判断" in result.output
    assert "重大公告未能从权威来源核实" in result.output
    assert "置信度" not in result.output
    assert "已保存" not in result.output
