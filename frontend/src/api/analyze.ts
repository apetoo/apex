/**
 * Analyze API
 *
 * ED11 analyze trace 事件(后端 analyze.py:run 通过 on_progress emit):
 *   { type: "context", name, content }             - 上下文注入
 *   { type: "tool_call", name, args, iteration }   - 调工具
 *   { type: "tool_result", name, result, iteration } - 工具返回
 *   { type: "assistant_text", content, iteration }  - AI 文字
 *   { type: "verdict", ... }                         - 最终 verdict(内嵌在 trace 事件中, done 事件也带)
 *   { error: ... }                                   - 失败
 *
 * 后端 SSE: GET /api/analyze/run?ts_code=xxx&save=true
 *   events: trace(以上) + done(verdict dict) + error
 * done.data = 完整 verdict dict
 */

import { api } from "./client";

export function analyzeRunPath(tsCode: string, save: boolean = true): string {
  // 必须带 /api 前缀 -- Vite proxy 只转发 /api 到 FastAPI,
  // 否则请求落到 dev server SPA fallback,返回 304/403/502(useSSE 报 HTTP <status>)。
  return `/api/analyze/run?ts_code=${encodeURIComponent(tsCode)}&save=${save}`;
}

/**
 * 适配 useSSE 的 fetchFn 形状(参数化路径与方法):
 * useSSE.connect({ path, method, body, signal, fetchFn })
 * fetchFn 收到 { path, method, body, signal } 返回 Promise<Response>
 */
export function analyzeFetchFn() {
  return async (params: {
    path: string;
    method: string;
    body: unknown;
    signal: AbortSignal;
  }): Promise<Response> => {
    return fetch(params.path, {
      method: params.method,
      headers: params.body ? { "Content-Type": "application/json" } : undefined,
      body: params.body ? JSON.stringify(params.body) : undefined,
      signal: params.signal,
    });
  };
}

/** GET /api/journal - 全部标的历史分析(跨股, 倒序)。供 /journal 历史页。 */
export async function getAllJournal(): Promise<unknown[]> {
  return api.get<unknown[]>("/journal");
}

/** GET /api/journal/{ts_code} - 历史 journal 列表 */
export async function getJournal(tsCode: string): Promise<unknown[]> {
  return api.get<unknown[]>(`/journal/${encodeURIComponent(tsCode)}`);
}

/**
 * GET /api/journal/{ts_code}/latest - 最近一条 AI 分析记录。无记录返回 null。
 *
 * 返回 verdict entry(形状见 apex/analyze.py:run),其中 price_advice 含
 * entry/stop_loss/target。用于手动持仓同步 AI advice。
 */
export interface LatestJournal {
  ts_code: string;
  analysis_status?: "completed" | "insufficient_evidence";
  analyzed_at?: string;
  source?: string;
  verdict?: string;
  confidence?: number;
  calibrated_confidence?: number;
  evidence?: Array<string | EvidenceItem>;
  unknowns?: string[];
  research_summary?: string;
  price_advice?: {
    entry?: number | null;
    entry_low?: number | null;
    entry_high?: number | null;
    stop_loss?: number | null;
    target?: number | null;
  };
  position_action?: {
    action?: string;
    add_shares?: number | null;
    trim_shares?: number | null;
    trim_pct?: number | null;
    new_stop?: number | null;
    rationale?: string;
    scale_plan?: unknown[];
  };
}

export interface EvidenceItem {
  id: string;
  fact: string;
  inference?: string;
  evidence_type?: string;
  tool_name?: string;
  source_name?: string;
  source_url?: string;
  published_at?: string | null;
  source_tier?: number;
  entity_matched?: boolean;
  freshness_status?: string;
}

export async function getLatestJournal(
  tsCode: string,
): Promise<LatestJournal | null> {
  return api.get<LatestJournal | null>(
    `/journal/${encodeURIComponent(tsCode)}/latest`,
  );
}

/**
 * GET /api/trace/{ts_code}/{analyzed_at} - 某次分析的完整事件流(trace.jsonl)。
 *
 * 回放历史分析过程用。无记录返回 null(早期分析未落 trace / save=False)。
 * events 形状见 <TraceEventList> 注释(对齐真后端 apex/analyze.py:_emit)。
 * 这里用 unknown[] 而非 TraceEvent[] -- 避免 api 层反向依赖 components 层,
 * 组件层自行断言。
 */
export interface TraceRecord {
  ts_code: string;
  analyzed_at: string;
  events: Record<string, unknown>[];
}

export async function getTrace(
  tsCode: string,
  analyzedAt: string,
): Promise<TraceRecord | null> {
  return api.get<TraceRecord | null>(
    `/trace/${encodeURIComponent(tsCode)}/${encodeURIComponent(analyzedAt)}`,
  );
}
