/**
 * Chat API
 *
 * POST /api/chat/stream
 *   body: { message, history }
 *   SSE: { chunk: { content } } | { done } | { error }
 */

interface ChatMsg {
  role: "user" | "assistant";
  content: string;
}

export function chatFetchFn(message: string, history: ChatMsg[]) {
  return async (params: {
    path: string;
    method: string;
    body: unknown;
    signal: AbortSignal;
  }): Promise<Response> => {
    return fetch(params.path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history }),
      signal: params.signal,
    });
  };
}
