"""SSE 流式封装。

apex 的流式输出分两类：

1. **回调型**（``apex.analyze.run`` / ``apex.screener.run``）
   一次阻塞调用，过程中通过 ``on_progress`` 回调吐事件。
   用线程跑阻塞调用 + ``queue.Queue`` 跨线程传事件，异步生成器逐条 yield SSE。

2. **生成器型**（``apex.llm.chat_stream``）
   已是同步生成器，用 ``run_in_executor`` 包 ``next`` 逐块 yield。

所有生成器产出 ``{"event", "data"}`` dict，交给 ``sse_starlette.EventSourceResponse``。
"""
from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any, AsyncIterator, Callable

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
            yield {"event": trace_event, "data": payload}
        elif kind == "done":
            yield {"event": done_event, "data": payload}
            return
        else:  # error
            yield {"event": "error", "data": {"error": str(payload)}}
            return


# ── 生成器型 ──────────────────────────────────────────────────────────────────


async def stream_generator(gen_factory: Callable[..., Any]) -> AsyncIterator[dict]:
    """跑一个同步生成器，逐块 yield ``chunk`` 事件，结束 yield ``done``。"""
    loop = asyncio.get_running_loop()
    gen = gen_factory()
    while True:
        try:
            chunk = await loop.run_in_executor(None, next, gen)
        except StopIteration:
            break
        except Exception as exc:  # noqa: BLE001
            yield {"event": "error", "data": {"error": str(exc)}}
            return
        yield {"event": "chunk", "data": {"content": chunk}}
    yield {"event": "done", "data": {}}
