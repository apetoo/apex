/**
 * useSSE<T> — SSE 客户端 hook
 *
 * ED1(修正, outside voice #1):
 *   - **都关自动重连**(GET 和 POST 都关)。原因:
 *     - analyze(GET)非幂等(save=True), 重连会重跑 DeepSeek + 重复写 journal
 *     - chat/screener(POST)丢 body
 *   - 断流渲染「连接断开 · 点击重试」, 由用户手动重发(带完整 body)
 *
 * ED8(修正, outside voice #7): 不依赖 MSW(对 fetch-event-source 流式拦截脆弱),
 * 单测用手写 ReadableStream mock 测边界。
 *
 * 流式事件(按后端事件名, 见 backend/core/streaming.py):
 *   - analyze: { trace, done, error }   — GET /api/analyze/run
 *   - chat:    { chunk, done, error }   — POST /api/chat/stream
 *   - screener:{ trace, done, error }(trace.data.type==="progress" 表示进度) — POST /api/screener/run
 *
 * T = 事件 union 类型, 调用方传泛型区分不同 stream。
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type SSEEvent<T> = {
  event: string; // 后端 event 名: trace / chunk / done / error
  data: T; // 后端 data 字段
};

export type SSEState<T> = {
  /** 当前已收到的事件流(累积, 不去重) */
  events: SSEEvent<T>[];
  /** 最新 done 事件的 data(终态) */
  result: T | null;
  /** 错误信息(网络/解析/error 事件) */
  error: string | null;
  /** 状态: idle / connecting / streaming / done / disconnected */
  status: "idle" | "connecting" | "streaming" | "done" | "disconnected";
};

export interface SSERequest {
  /** 请求路径, e.g. /api/analyze/run?ts_code=000001.SZ */
  path: string;
  /** POST body(GET 可不传)。重试时携带完整 body。 */
  body?: unknown;
  /** GET / POST */
  method?: "GET" | "POST";
  /** AbortSignal(外部中断, 如组件卸载) */
  signal?: AbortSignal;
  /**
   * 自定义 fetcher(默认 globalThis.fetch)。dev mock 注入, 测试注入。
   * 必须返回带 body.getReader() 的 Response, signal 已带(req.signal)。
   */
  fetchFn?: (req: { path: string; method: string; body: unknown; signal: AbortSignal }) => Promise<Response>;
}

/** 简单 JSON 解析: 后端 SSE 事件 data 字段是 JSON 字符串 */
function parseSSEData<T>(raw: string): T {
  if (raw === "[DONE]") return null as T;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return raw as unknown as T;
  }
}

/** 默认 fetcher(相对路径经 Vite proxy 转发到后端) */
async function defaultFetch(req: {
  path: string;
  method: string;
  body: unknown;
  signal: AbortSignal;
}): Promise<Response> {
  return fetch(req.path, {
    method: req.method,
    headers: req.body ? { "Content-Type": "application/json" } : undefined,
    body: req.body ? JSON.stringify(req.body) : undefined,
    signal: req.signal,
  });
}

/** 解析一行 SSE: "event: trace\ndata: {...}\n\n" */
function parseSSELine(line: string): { event?: string; data?: string } {
  if (line.startsWith("event: ")) return { event: line.slice(7).trim() };
  if (line.startsWith("data: ")) return { data: line.slice(6).trim() };
  return {};
}

/**
 * 解析 SSE 文本块为事件数组
 * SSE 格式: 每个事件以 \n\n 分隔, 多行 event: + data: 组成
 */
function parseSSEChunk<T>(buffer: string): {
  events: SSEEvent<T>[];
  rest: string;
} {
  const events: SSEEvent<T>[] = [];
  // sse_starlette 行尾 \r\n(事件间 \r\n\r\n); 用 /\r?\n\r?\n/ 兼容 \n (测试用例),
  // 否则真后端事件全堆 buffer。
  const parts = buffer.split(/\r?\n\r?\n/);
  // 最后一段可能不完整, 留作 buffer
  const rest = parts.pop() ?? "";

  for (const part of parts) {
    if (!part.trim()) continue;
    let eventName: string | undefined;
    let dataRaw: string | undefined;
    for (const line of part.split(/\r?\n/)) {
      const parsed = parseSSELine(line);
      if (parsed.event) eventName = parsed.event;
      if (parsed.data !== undefined) dataRaw = parsed.data;
    }
    if (eventName && dataRaw !== undefined) {
      events.push({ event: eventName, data: parseSSEData(dataRaw) });
    }
  }

  return { events, rest };
}

export function useSSE<T = unknown>() {
  const [state, setState] = useState<SSEState<T>>({
    events: [],
    result: null,
    error: null,
    status: "idle",
  });

  // 当前请求的 AbortController(中断用)
  const abortRef = useRef<AbortController | null>(null);
  // buffer 中残留未完整解析的文本
  const bufferRef = useRef<string>("");

  /** 启动一次 SSE 连接。返回 AbortController(供外部中断)。 */
  const connect = useCallback(async (req: SSERequest) => {
    // 取消上一次未完成
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    if (req.signal) {
      req.signal.addEventListener("abort", () => ctrl.abort());
    }

    setState({
      events: [],
      result: null,
      error: null,
      status: "connecting",
    });
    bufferRef.current = "";

    try {
      const doFetch = req.fetchFn ?? defaultFetch;
      const res = await doFetch({
        path: req.path,
        method: req.method ?? "GET",
        body: req.body ?? null,
        signal: ctrl.signal,
      });

      if (!res.ok) {
        const text = await res.text().catch(() => res.statusText);
        setState({
          events: [],
          result: null,
          error: `HTTP ${res.status}: ${text}`,
          status: "disconnected",
        });
        return ctrl;
      }

      const reader = res.body?.getReader();
      if (!reader) {
        setState({
          events: [],
          result: null,
          error: "Response body is empty",
          status: "disconnected",
        });
        return ctrl;
      }

      const decoder = new TextDecoder();
      setState((s) => ({ ...s, status: "streaming" }));

      let finished = false;
      while (!finished) {
        const { value, done } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        bufferRef.current += chunk;
        const { events, rest } = parseSSEChunk<T>(bufferRef.current);
        bufferRef.current = rest;

        if (events.length > 0) {
          setState((s) => {
            const lastDone = events.find((e) => e.event === "done");
            const lastError = events.find((e) => e.event === "error");
            return {
              events: [...s.events, ...events],
              result: lastDone ? lastDone.data : s.result,
              error: lastError
                ? typeof lastError.data === "string"
                  ? lastError.data
                  : JSON.stringify(lastError.data)
                : s.error,
              status: lastDone
                ? "done"
                : lastError
                  ? "disconnected"
                  : "streaming",
            };
          });
          if (events.some((e) => e.event === "done" || e.event === "error")) {
            finished = true;
            void reader.cancel();
          }
        }
      }

      setState((s) =>
        s.status === "streaming" ? { ...s, status: "done" } : s,
      );
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        setState((s) =>
          s.status === "done" ? s : { ...s, status: "idle" },
        );
        return ctrl;
      }
      setState({
        events: [],
        result: null,
        error: (err as Error).message ?? "Network error",
        status: "disconnected",
      });
    }

    return ctrl;
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setState({
      events: [],
      result: null,
      error: null,
      status: "idle",
    });
    bufferRef.current = "";
  }, []);

  const abort = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  return {
    ...state,
    connect,
    reset,
    abort,
  };
}
