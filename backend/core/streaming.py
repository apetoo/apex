"""SSE 流式封装。

apex 的流式输出分两类：

1. **回调型**（``apex.analyze.run`` / ``apex.screener.run``）
   一次阻塞调用，过程中通过 ``on_progress`` 回调吐事件。
   用线程跑阻塞调用 + ``queue.Queue`` 跨线程传事件，异步生成器逐条 yield SSE。

2. **生成器型**（``apex.llm.chat_stream``）
   已是同步生成器，用 ``run_in_executor`` 包 ``next`` 逐块 yield。

所有生成器产出 ``{"event", "data"}`` dict，交给 ``sse_starlette.EventSourceResponse``。

**SSE data contract（跨前后端，勿踩坑）**: sse_starlette 3.x 的 EventSourceResponse
对 ``data`` 直接 ``str()`` —— dict 会变 Python repr（单引号），前端 ``JSON.parse``
必失败。所以所有事件**必须走 ``_sse()``**，把 data 先 ``json.dumps`` 成字符串再 yield，
不要直接 ``yield {"event":..., "data": <dict>}``。
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import Any, AsyncIterator, Callable


def _sse(event: str, data: Any) -> dict:
    """构造一个 SSE 事件 dict，data 强制 json.dumps 成字符串。

    sse_starlette 对 dict data 会 str() 成单引号 repr，前端 JSON.parse 失败。
    这里先序列化成合法 JSON 字符串（ensure_ascii=False 保留中文，default=str 兜底）。
    """
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


# ── 回调型 ────────────────────────────────────────────────────────────────────


async def stream_callback(
    func: Callable,
    *args: Any,
    on_progress_kw: str = "on_progress",
    progress_wrapper: Callable[[Any], Any] | None = None,
    trace_event: str = "trace",
    done_event: str = "done",
    **kwargs: Any,
) -> AsyncIterator[dict]:
    """跑一个回调型阻塞函数，把 on_progress 事件 + 最终结果流式吐出。

    ``progress_wrapper`` 把 on_progress 的原始入参包成想要的 dict（例如
    screener 传的是字符串，包成 ``{"type": "progress", "message": m}``）。
    """
    q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
    loop = asyncio.get_running_loop()

    def _on_progress(payload: Any) -> None:
        if progress_wrapper is not None:
            payload = progress_wrapper(payload)
        q.put(("event", payload))

    def _runner() -> None:
        try:
            result = func(*args, **{on_progress_kw: _on_progress}, **kwargs)
            q.put(("done", result))
        except Exception as exc:  # noqa: BLE001 — 透传给 SSE error 事件
            q.put(("error", exc))

    threading.Thread(target=_runner, daemon=True).start()

    while True:
        kind, payload = await loop.run_in_executor(None, q.get)
        if kind == "event":
            yield _sse(trace_event, payload)
        elif kind == "done":
            yield _sse(done_event, payload)
            return
        else:  # error
            yield _sse("error", {"error": str(payload)})
            return


# ── 生成器型 ──────────────────────────────────────────────────────────────────

# 哨兵: run_in_executor 无法把 StopIteration 传回 asyncio Future
# (会变 RuntimeError: StopIteration interacts badly with generators),
# 所以在 executor 线程内把 StopIteration 转成哨兵, 主循环据此 break。
_SENTINEL = object()


def _next_or_sentinel(gen):
    """next(gen) 但把 StopIteration 转成哨兵, 不让异常逃出 executor 线程。"""
    try:
        return next(gen)
    except StopIteration:
        return _SENTINEL


async def stream_generator(gen_factory: Callable[..., Any]) -> AsyncIterator[dict]:
    """跑一个同步生成器，逐块 yield ``chunk`` 事件，结束 yield ``done``。

    生成器可 yield 两种值：
    - **字符串** -> 包成 ``chunk`` 事件（``{content}``），向后兼容（如 ``llm.chat_stream``）。
    - **dict** ``{"event": "...", "data": {...}}`` -> 透传为该 typed 事件
      （如 chat 的 ``tool_call`` / ``tool_result``）。data 经 ``_sse`` json.dumps。
    """
    loop = asyncio.get_running_loop()
    gen = gen_factory()
    while True:
        try:
            val = await loop.run_in_executor(None, _next_or_sentinel, gen)
        except Exception as exc:  # noqa: BLE001 — 生成器抛真实异常(如 API 错误)→ error 事件
            yield _sse("error", {"error": str(exc)})
            return
        if val is _SENTINEL:
            break
        if isinstance(val, dict) and "event" in val:
            yield _sse(val["event"], val.get("data", {}))
        else:
            yield _sse("chunk", {"content": val})
    yield _sse("done", {})
