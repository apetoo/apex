"""Optional LangSmith tracing for the stock-analysis workflow."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

import langsmith as _langsmith
from langchain_core.tracers.langchain import wait_for_all_tracers as _wait_for_all_tracers
from langsmith.wrappers import wrap_openai as _wrap_openai


_logger = logging.getLogger(__name__)
_trace = _langsmith.trace
_DEFAULT_PROJECT = "apex-ai-analysis"


def is_langsmith_enabled() -> bool:
    """Return whether analysis tracing was explicitly enabled and configured."""
    tracing = os.environ.get("LANGSMITH_TRACING", "").strip().lower()
    return tracing == "true" and bool(os.environ.get("LANGSMITH_API_KEY", "").strip())


def wrap_analysis_client(client):
    """Wrap only the analysis OpenAI client, falling back on observer failures."""
    if not is_langsmith_enabled():
        return client
    try:
        return _wrap_openai(
            client,
            tracing_extra={"tags": ["apex", "stock-analysis"]},
        )
    except Exception as exc:
        _logger.warning("LangSmith client 包装失败，继续使用原客户端: %s", exc)
        return client


def flush_analysis_traces() -> None:
    """Flush background trace uploads for short-lived CLI processes."""
    if not is_langsmith_enabled():
        return
    try:
        _wait_for_all_tracers()
    except Exception as exc:
        _logger.warning("LangSmith trace 刷新失败: %s", exc)


class TraceSpan:
    """Small observer-safe facade over a LangSmith run tree."""

    def __init__(self, run: Any | None) -> None:
        self._run = run

    def set_outputs(self, outputs: dict[str, Any]) -> None:
        if self._run is None:
            return
        try:
            self._run.end(outputs=outputs)
        except Exception as exc:
            _logger.warning("LangSmith trace 输出记录失败: %s", exc)


@contextmanager
def _safe_trace(**kwargs) -> Iterator[TraceSpan]:
    if not is_langsmith_enabled():
        yield TraceSpan(None)
        return

    try:
        manager = _trace(**kwargs)
        run = manager.__enter__()
    except Exception as exc:
        _logger.warning("LangSmith trace 创建失败，跳过本次上报: %s", exc)
        yield TraceSpan(None)
        return

    try:
        yield TraceSpan(run)
    except BaseException as business_error:
        try:
            manager.__exit__(
                type(business_error), business_error, business_error.__traceback__,
            )
        except Exception as observer_error:
            _logger.warning("LangSmith trace 异常上报失败: %s", observer_error)
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception as exc:
            _logger.warning("LangSmith trace 提交失败: %s", exc)


def analysis_trace(
    *,
    ts_code: str,
    save: bool,
    candidate_context: dict[str, Any],
    model: str,
    prompt_version: str,
    held: bool,
):
    """Create the parent span for one complete stock analysis."""
    source_type = candidate_context.get("strategy") or candidate_context.get("source_type")
    return _safe_trace(
        name="apex-stock-analysis",
        run_type="chain",
        inputs={
            "ts_code": ts_code,
            "save": save,
            "candidate_context": candidate_context,
        },
        tags=["apex", "stock-analysis", ts_code],
        metadata={
            "ts_code": ts_code,
            "model": model,
            "prompt_version": prompt_version,
            "source_type": source_type,
            "held": held,
        },
        project_name=os.environ.get("LANGSMITH_PROJECT", _DEFAULT_PROJECT),
    )


def tool_trace(name: str, inputs: dict[str, Any]):
    """Create a nested span for one Apex tool or internal decision tool."""
    return _safe_trace(
        name=f"tool:{name}",
        run_type="tool",
        inputs=inputs,
        tags=["apex", "stock-analysis", "tool", name],
        metadata={"tool_name": name},
        project_name=os.environ.get("LANGSMITH_PROJECT", _DEFAULT_PROJECT),
    )
