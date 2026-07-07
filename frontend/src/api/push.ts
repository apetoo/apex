/**
 * Push API — 持仓推送管理
 *
 * POST /api/push/full          手动触发全量推送
 * GET  /api/push/status        推送状态（开关 / last_seq / 末次结果）
 * GET  /api/push/log?limit=N   推送审计日志
 * POST /api/push/replay?since  重放 seq > since 的推送
 *
 * 接口文档：docs/integration/positions-push-api.md
 * 推送是真实后端动作，不提供 mock。
 */

import { api } from "./client";

export interface PushStatus {
  enabled: boolean;
  incremental_enabled: boolean;
  base_url: string;
  last_seq: number;
  last_push_at: string | null;
  last_status: string | null;
  last_http_status: number | null;
}

export interface PushResult {
  status: string;
  http_status: number | null;
  attempts: number;
  error?: string | null;
  latency_ms?: number;
}

export interface PushLogEntry {
  push_id: string;
  seq: number;
  push_type: string;
  event_type: string | null;
  ts_code: string | null;
  attempt: number;
  status: string;
  http_status: number | null;
  error: string | null;
  latency_ms: number;
  sent_at: string;
}

/** GET /api/push/status */
export function getPushStatus(): Promise<PushStatus> {
  return api.get<PushStatus>("/push/status");
}

/** POST /api/push/full — 手动触发全量推送，同步返回发送结果 */
export function triggerFullPush(): Promise<PushResult> {
  return api.post<PushResult>("/push/full");
}

/** GET /api/push/log?limit=N */
export function getPushLog(limit = 100): Promise<PushLogEntry[]> {
  return api.get<PushLogEntry[]>(`/push/log?limit=${limit}`);
}

/** POST /api/push/replay?since=N */
export function replayPush(
  since = 0,
): Promise<{ replayed: number; results: unknown[] }> {
  return api.post(`/push/replay?since=${since}`);
}
