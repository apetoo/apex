import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useSSE } from "../useSSE";

/**
 * useSSE<T> 边界单测(ED8 修正)
 *
 * 不依赖 MSW(对 fetch-event-source 流式拦截脆弱), 手写 ReadableStream mock。
 * 覆盖 ED1 决策: GET/POST 都关自动重连 + 手动重试; error 事件渲染; 可中断。
 */

function sseText(events: Array<{ event: string; data: unknown }>): string {
  return events
    .map(
      (e) =>
        `event: ${e.event}\ndata: ${typeof e.data === "string" ? e.data : JSON.stringify(e.data)}\n\n`,
    )
    .join("");
}

function stringToStream(text: string): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(text));
      controller.close();
    },
  });
}

function chunksToStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    async pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i]));
        i++;
        await new Promise((r) => setTimeout(r, 0));
      } else {
        controller.close();
      }
    },
  });
}

describe("useSSE", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  function mockFetchOnce(body: ReadableStream<Uint8Array>, init?: ResponseInit) {
    return fetchSpy.mockResolvedValueOnce(
      new Response(body, { status: 200, ...init }) as unknown as Response,
    );
  }

  it("GET analyze 流式: trace 事件累积 + done 终态", async () => {
    const text = sseText([
      { event: "trace", data: { type: "tool_call", name: "get_daily" } },
      { event: "trace", data: { type: "tool_result", name: "get_daily" } },
      { event: "trace", data: { type: "assistant_text", content: "看多" } },
      { event: "done", data: { verdict: "看多" } },
    ]);
    mockFetchOnce(stringToStream(text));

    const { result } = renderHook(() => useSSE<{ verdict: string }>());

    await act(async () => {
      await result.current.connect({
        path: "/api/analyze/run?ts_code=000001.SZ",
        method: "GET",
      });
    });

    await waitFor(() => {
      expect(result.current.status).toBe("done");
    });
    expect(result.current.events).toHaveLength(4);
    expect(result.current.events[0].event).toBe("trace");
    expect(result.current.events[3].event).toBe("done");
    expect(result.current.result).toEqual({ verdict: "看多" });
    expect(result.current.error).toBeNull();
  });

  it("POST chat 流式: chunk 事件累积 + done 终态", async () => {
    const text = sseText([
      { event: "chunk", data: { delta: "你好" } },
      { event: "chunk", data: { delta: "，" } },
      { event: "chunk", data: { delta: "我是 AI" } },
      { event: "done", data: { full: "你好，我是 AI" } },
    ]);
    mockFetchOnce(stringToStream(text));

    const { result } = renderHook(() => useSSE<{ delta: string } | { full: string }>());

    await act(async () => {
      await result.current.connect({
        path: "/api/chat/stream",
        method: "POST",
        body: { message: "hi", history: [] },
      });
    });

    await waitFor(() => {
      expect(result.current.status).toBe("done");
    });
    expect(result.current.events).toHaveLength(4);
    expect(result.current.result).toEqual({ full: "你好，我是 AI" });

    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/chat/stream",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ message: "hi", history: [] }),
      }),
    );
  });

  it("error 事件: status=disconnected, error 字段填充", async () => {
    const text = sseText([
      { event: "trace", data: { step: 1 } },
      { event: "error", data: { error: "DeepSeek API timeout" } },
    ]);
    mockFetchOnce(stringToStream(text));

    const { result } = renderHook(() => useSSE<{ step: number } | { error: string }>());

    await act(async () => {
      await result.current.connect({ path: "/api/analyze/run" });
    });

    await waitFor(() => {
      expect(result.current.status).toBe("disconnected");
    });
    expect(result.current.error).toContain("DeepSeek API timeout");
  });

  it("HTTP 4xx: 立即 disconnected(无 fetch 抛错)", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response("Not Found", { status: 404 }) as unknown as Response,
    );

    const { result } = renderHook(() => useSSE());

    await act(async () => {
      await result.current.connect({ path: "/api/missing" });
    });

    await waitFor(() => {
      expect(result.current.status).toBe("disconnected");
    });
    expect(result.current.error).toMatch(/HTTP 404/);
  });

  it("可中断: abort() 后 status 变 idle/disconnected(非 done 时)", async () => {
    const neverClose = new ReadableStream<Uint8Array>({
      start() {
        // 不 enqueue 也不 close
      },
    });
    mockFetchOnce(neverClose);

    const { result } = renderHook(() => useSSE());

    // 启动连接(不 await), 让 setState 在背后跑
    act(() => {
      void result.current.connect({ path: "/api/long" });
    });

    // 等 React 把 setState 应用
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });

    act(() => {
      result.current.abort();
    });

    // 等 React flush abort 引发的 setState + reader reject
    await waitFor(
      () => {
        // 关键断言: AbortError 不被当 error(应该 null,不是 AbortError 字符串)
        expect(result.current.error).toBeNull();
        // status 可能是 idle/disconnected/streaming(取决于 race), 都不能是 done
        expect(result.current.status).not.toBe("done");
      },
      { timeout: 1000 },
    );
  });

  it("重置: reset() 清空 events / result / error", async () => {
    const text = sseText([{ event: "done", data: { verdict: "看多" } }]);
    mockFetchOnce(stringToStream(text));

    const { result } = renderHook(() => useSSE<{ verdict: string }>());

    act(() => {
      void result.current.connect({ path: "/api/analyze/run" });
    });

    await waitFor(() => {
      expect(result.current.status).toBe("done");
    });
    expect(result.current.result).toEqual({ verdict: "看多" });

    act(() => {
      result.current.reset();
    });

    expect(result.current.events).toHaveLength(0);
    expect(result.current.result).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.status).toBe("idle");
  });

  it("ED1 关键断言: 不自动重连(断流不会重发 fetch)", async () => {
    // 流只发一半就关闭 = 断流场景, fetch 不应被调第二次
    const halfStream = chunksToStream([
      'event: trace\ndata: {"step":1}\n\n',
    ]);
    mockFetchOnce(halfStream);

    const { result } = renderHook(() => useSSE<{ step: number }>());

    act(() => {
      void result.current.connect({ path: "/api/analyze/run" });
    });

    // 等异步 settle
    await act(async () => {
      await new Promise((r) => setTimeout(r, 100));
    });

    // ED1: fetch 只被调一次
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });
});
