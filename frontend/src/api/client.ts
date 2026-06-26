/**
 * API fetch 封装
 *
 * ED6 硬约束: 组件层禁止直接 fetch('/api/...'), 必须走此 api/ 层。
 * 后端返回的 JSON 字符串已在 backend.core.response.parse_json 解包,
 * 前端拿到的都是真实对象。
 */

const BASE = "/api";

export class ApiError extends Error {
  status: number;
  body?: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      body = await res.text().catch(() => undefined);
    }
    const msg =
      (body && typeof body === "object" && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : typeof body === "string"
          ? body
          : res.statusText) ?? res.statusText;
    throw new ApiError(res.status, msg, body);
  }

  // 204 或空 body
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  /** SSE 用: 不解析 JSON, 直接返回 Response 让 useSSE 读流 */
  getRaw: (path: string) => fetch(`${BASE}${path}`),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      body: body == null ? undefined : JSON.stringify(body),
    }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "PUT",
      body: body == null ? undefined : JSON.stringify(body),
    }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
