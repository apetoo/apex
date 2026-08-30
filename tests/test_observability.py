import pytest
from contextlib import contextmanager

from apex import analyze, observability


class _FakeRun:
    def __init__(self):
        self.outputs = None

    def end(self, *, outputs=None, error=None):
        self.outputs = outputs


class _FakeTraceContext:
    def __init__(self, run, *, exit_error=None):
        self.run = run
        self.exit_error = exit_error
        self.exited_with = None

    def __enter__(self):
        return self.run

    def __exit__(self, exc_type, exc, traceback):
        self.exited_with = exc
        if self.exit_error:
            raise self.exit_error
        return False


def test_langsmith_requires_explicit_enable_and_api_key(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    assert observability.is_langsmith_enabled() is False

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert observability.is_langsmith_enabled() is False

    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    assert observability.is_langsmith_enabled() is True


def test_disabled_client_wrapper_returns_original_client(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    client = object()
    assert observability.wrap_analysis_client(client) is client


def test_enabled_client_wrapper_passes_analysis_tags(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    calls = []
    wrapped = object()
    monkeypatch.setattr(
        observability,
        "_wrap_openai",
        lambda client, **kwargs: calls.append((client, kwargs)) or wrapped,
    )

    client = object()
    assert observability.wrap_analysis_client(client) is wrapped
    assert calls == [(client, {"tracing_extra": {"tags": ["apex", "stock-analysis"]}})]


def test_analysis_trace_records_outputs_and_metadata(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setenv("LANGSMITH_PROJECT", "test-apex")
    run = _FakeRun()
    captured = []

    def fake_trace(**kwargs):
        captured.append(kwargs)
        return _FakeTraceContext(run)

    monkeypatch.setattr(observability, "_trace", fake_trace)

    with observability.analysis_trace(
        ts_code="000977.SZ",
        save=False,
        candidate_context={"source_type": "manual"},
        model="deepseek-chat",
        prompt_version="3.0.0-langgraph",
        held=True,
    ) as span:
        span.set_outputs({"analysis_status": "completed", "verdict": "偏多"})

    assert captured == [{
        "name": "apex-stock-analysis",
        "run_type": "chain",
        "inputs": {
            "ts_code": "000977.SZ",
            "save": False,
            "candidate_context": {"source_type": "manual"},
        },
        "tags": ["apex", "stock-analysis", "000977.SZ"],
        "metadata": {
            "ts_code": "000977.SZ",
            "model": "deepseek-chat",
            "prompt_version": "3.0.0-langgraph",
            "source_type": "manual",
            "held": True,
        },
        "project_name": "test-apex",
    }]
    assert run.outputs == {"analysis_status": "completed", "verdict": "偏多"}


def test_tool_trace_records_result(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    run = _FakeRun()
    captured = []
    monkeypatch.setattr(
        observability,
        "_trace",
        lambda **kwargs: captured.append(kwargs) or _FakeTraceContext(run),
    )

    with observability.tool_trace("get_daily_price", {"ts_code": "000977.SZ"}) as span:
        span.set_outputs({"result": "raw-result"})

    assert captured[0]["name"] == "tool:get_daily_price"
    assert captured[0]["run_type"] == "tool"
    assert captured[0]["inputs"] == {"ts_code": "000977.SZ"}
    assert run.outputs == {"result": "raw-result"}


def test_trace_exit_failure_does_not_replace_business_error(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setattr(
        observability,
        "_trace",
        lambda **_kwargs: _FakeTraceContext(_FakeRun(), exit_error=RuntimeError("upload failed")),
    )

    with pytest.raises(ValueError, match="tool failed"):
        with observability.tool_trace("broken_tool", {}):
            raise ValueError("tool failed")


def test_analysis_client_is_wrapped_without_changing_shared_llm_factory(monkeypatch):
    raw_client = object()
    wrapped_client = object()
    monkeypatch.setattr("apex.llm.make_client", lambda **_kwargs: raw_client)
    monkeypatch.setattr(
        observability,
        "wrap_analysis_client",
        lambda client: wrapped_client if client is raw_client else client,
    )

    result = analyze._make_client({
        "deepseek": {"api_key": "secret", "base_url": "https://example.invalid"},
    })

    assert result is wrapped_client


def test_run_creates_parent_trace_and_records_terminal_summary(monkeypatch):
    captured = {}

    class _Span:
        def set_outputs(self, outputs):
            captured["outputs"] = outputs

    @contextmanager
    def fake_analysis_trace(**kwargs):
        captured["trace"] = kwargs
        yield _Span()

    monkeypatch.setattr(analyze._cfg_mod, "get", lambda: {
        "deepseek": {"model": "deepseek-chat"},
    })
    monkeypatch.setattr("apex.watchlist.load", lambda: {
        "active_positions": [{"ts_code": "000977.SZ"}],
    })
    monkeypatch.setattr(observability, "analysis_trace", fake_analysis_trace)
    monkeypatch.setattr(analyze, "_run_analysis", lambda *args, **kwargs: {
        "analysis_status": "completed",
        "verdict": "偏多",
        "token_usage": {"calls": 3},
    }, raising=False)

    result = analyze.run(
        "000977.SZ", save=False,
        candidate_context={"source_type": "manual", "red_flag": False},
    )

    assert result["verdict"] == "偏多"
    assert captured["trace"] == {
        "ts_code": "000977.SZ",
        "save": False,
        "candidate_context": {"source_type": "manual", "red_flag": False},
        "model": "deepseek-chat",
        "prompt_version": "3.0.0-langgraph",
        "held": True,
    }
    assert captured["outputs"] == {
        "analysis_status": "completed",
        "outcome_reason": None,
        "verdict": "偏多",
        "action": None,
        "token_usage": {"calls": 3},
    }


def test_graph_invocation_is_named_and_searchable(monkeypatch):
    captured = {}

    class _Graph:
        def invoke(self, state, config):
            captured["state"] = state
            captured["config"] = config
            return {"analysis_status": "completed"}

    monkeypatch.setattr(analyze.data, "get_name_map", lambda: {"000977.SZ": "浪潮信息"})
    monkeypatch.setattr(analyze, "build_analysis_graph", lambda _handlers: _Graph())

    result = analyze._run_langgraph_loop(
        ts_code="000977.SZ",
        client=object(),
        model="deepseek-chat",
        messages=[{"role": "user", "content": "分析"}],
        max_iter=12,
        emit=lambda _event: None,
    )

    assert result == {"analysis_status": "completed"}
    assert captured["state"] == {}
    assert captured["config"] == {
        "recursion_limit": 60,
        "run_name": "apex-analysis-graph",
        "tags": ["apex", "stock-analysis", "000977.SZ"],
        "metadata": {"ts_code": "000977.SZ", "model": "deepseek-chat"},
    }


def test_flush_waits_only_when_tracing_is_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(observability, "_wait_for_all_tracers", lambda: calls.append("flush"), raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)

    observability.flush_analysis_traces()
    assert calls == []

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    observability.flush_analysis_traces()
    assert calls == ["flush"]
