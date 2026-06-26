/**
 * Chat API
 *
 * POST /api/chat/stream
 *   body: { message, history }
 *   SSE: { chunk: { content } } | { done } | { error }
 *
 * dev mock: 注入到 useSSE 的 fetchFn。
 */

const USE_MOCK = import.meta.env.VITE_USE_MOCK !== "0";

interface ChatMsg {
  role: "user" | "assistant";
  content: string;
}

const MOCK_REPLIES = [
  "我来分析一下当前持仓和板块情况。",
  "今天天齐锂业受锂电板块情绪带动, 量能放大 30%, MA20 支撑有效。",
  "建议: 若回踩 67.5 不破可加仓, 止损上移到 65.0 锁住 2.5% 风险。",
];

function sseEncode(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

export function buildMockChatFetcher(message: string) {
  return async (): Promise<Response> => {
    const encoder = new TextEncoder();
    // 简单回声: 把用户消息 + 一段 AI 回复拼起来
    const reply = `你说:「${message}」\n\n` + MOCK_REPLIES.join("");

    const stream = new ReadableStream<Uint8Array>({
      async start(controller) {
        // 拆分成 ~10 个 chunk, 每个 150ms, 模拟 token-by-token
        const chunks = reply.match(/.{1,8}/g) ?? [reply];
        for (const c of chunks) {
          await new Promise((r) => setTimeout(r, 150));
          controller.enqueue(
            encoder.encode(sseEncode("chunk", { content: c })),
          );
        }
        await new Promise((r) => setTimeout(r, 200));
        controller.enqueue(encoder.encode(sseEncode("done", { full: reply })));
        controller.close();
      },
    });
    return new Response(stream, { status: 200 });
  };
}

export function chatFetchFn(message: string, history: ChatMsg[]) {
  return async (params: {
    path: string;
    method: string;
    body: unknown;
    signal: AbortSignal;
  }): Promise<Response> => {
    if (USE_MOCK) {
      return buildMockChatFetcher(message)();
    }
    return fetch(params.path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history }),
      signal: params.signal,
    });
  };
}
