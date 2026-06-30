/**
 * Analyze API
 *
 * ED11 analyze trace 事件(后端 analyze.py:run 通过 on_progress emit):
 *   { type: "context", name, content }             — 上下文注入
 *   { type: "tool_call", name, args, iteration }   — 调工具
 *   { type: "tool_result", name, result, iteration } — 工具返回
 *   { type: "assistant_text", content, iteration }  — AI 文字
 *   { type: "verdict", ... }                         — 最终 verdict(内嵌在 trace 事件中, done 事件也带)
 *   { error: ... }                                   — 失败
 *
 * 后端 SSE: GET /api/analyze/run?ts_code=xxx&save=true
 *   events: trace(以上) + done(verdict dict) + error
 * done.data = 完整 verdict dict
 *
 * dev mock: 注入到 useSSE 的 fetchFn, 返回本地 ReadableStream 模拟后端流。
 */

import { api } from "./client";

const USE_MOCK = import.meta.env.VITE_USE_MOCK !== "0";

export function analyzeRunPath(tsCode: string, save: boolean = true): string {
  // 必须带 /api 前缀 —— Vite proxy 只转发 /api 到 FastAPI,
  // 否则请求落到 dev server SPA fallback,返回 304/403/502(useSSE 报 HTTP <status>)。
  return `/api/analyze/run?ts_code=${encodeURIComponent(tsCode)}&save=${save}`;
}

const MOCK_TRACE_EVENTS: Array<Record<string, unknown>> = [
  {
    type: "context",
    name: "history",
    content: "该标的过往 3 次判断: 2026-05-12 偏多(命中, +8.2%); 2026-04-30 中性; 2026-04-15 偏多(命中, +5.1%)",
  },
  {
    type: "context",
    name: "portfolio",
    content: "当前持仓 100 股, 入场价 66.04, 止损 60.71, 目标 72.00",
  },
  {
    type: "tool_call",
    iteration: 0,
    name: "get_daily",
    args: { ts_code: "002466.SZ" },
  },
  {
    type: "tool_result",
    iteration: 0,
    name: "get_daily",
    result: { close: 68.5, ma20: 67.2, vol: 12345678 },
  },
  {
    type: "tool_call",
    iteration: 1,
    name: "get_fundamentals",
    args: { ts_code: "002466.SZ" },
  },
  {
    type: "tool_result",
    iteration: 1,
    name: "get_fundamentals",
    result: { pe_ttm: 18.5, circ_mv: 890, industry: "锂电" },
  },
  {
    type: "assistant_text",
    iteration: 2,
    content: "基本面稳健, 锂电板块情绪转暖, 技术面站上 MA20。",
  },
  {
    type: "verdict",
    verdict: "看多",
    confidence: 3.5,
    entry_price: 68.5,
    stop_loss: 65.0,
    target: 75.0,
    horizon: "swing",
    regime: "bull",
    evidence: [
      "MA20 支撑 → 多头趋势延续",
      "PE-TTM 18.5 → 估值合理偏低",
      "近 3 日成交量放大 30% → 资金关注",
    ],
  },
];

const MOCK_VERDICT: Record<string, unknown> = {
  ts_code: "002466.SZ",
  analyzed_at: "2026-06-28T10:30:00",
  verdict: "看多",
  confidence: 4,
  calibrated_confidence: 3.5,
  calibration_explanation: "同行业已持仓 2 只 → 置信度 -0.5",
  price_advice: {
    entry: 68.5,
    stop_loss: 65.0,
    target: 75.0,
    position_size_pct: 15,
  },
  evidence: [
    "MA20 支撑 → 多头趋势延续",
    "PE-TTM 18.5 → 估值合理偏低",
    "近 3 日成交量放大 30% → 资金关注",
  ],
  analysis_text:
    "## 结论\n**看多**，置信度 4（校准 3.5）。\n\n### 依据\n- 技术面站上 MA20，趋势转多\n- 估值 PE-TTM 18.5 处于历史中低位\n- 量能放大，资金关注度高\n\n### 风险\n- 同行业已持仓 2 只，集中度偏高，建议小仓位跟进",
};

function sseEncode(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/**
 * dev mock: 构造 SSE 流, 事件间隔 200ms 模拟流式节奏。
 * useSSE 通过 fetchFn 注入, 不污染全局 fetch。
 */
export function buildMockAnalyzeFetcher() {
  return async (): Promise<Response> => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      async start(controller) {
        for (const evt of MOCK_TRACE_EVENTS) {
          await new Promise((r) => setTimeout(r, 200));
          controller.enqueue(encoder.encode(sseEncode("trace", evt)));
        }
        await new Promise((r) => setTimeout(r, 200));
        controller.enqueue(encoder.encode(sseEncode("done", MOCK_VERDICT)));
        controller.close();
      },
    });
    return new Response(stream, { status: 200 });
  };
}

/**
 * 适配 useSSE 的 fetchFn 形状(参数化路径与方法):
 * useSSE.connect({ path, method, body, signal, fetchFn })
 * fetchFn 收到 { path, method, body, signal } 返回 Promise<Response>
 *
 * USE_MOCK 控制(mock 模式走本地 stream, 真实后端走原生 fetch)
 */
export function analyzeFetchFn() {
  return async (params: {
    path: string;
    method: string;
    body: unknown;
    signal: AbortSignal;
  }): Promise<Response> => {
    if (USE_MOCK) {
      return buildMockAnalyzeFetcher()();
    }
    return fetch(params.path, {
      method: params.method,
      headers: params.body ? { "Content-Type": "application/json" } : undefined,
      body: params.body ? JSON.stringify(params.body) : undefined,
      signal: params.signal,
    });
  };
}

/** 显式 mock 入口(不受 USE_MOCK 全局影响, 用于 demo) */
export function analyzeMockFetchFn() {
  return async (): Promise<Response> => buildMockAnalyzeFetcher()();
}

/** GET /api/journal — 全部标的历史分析(跨股, 倒序)。供 /journal 历史页。 */
export async function getAllJournal(): Promise<unknown[]> {
  if (USE_MOCK) {
    return [
      {
        ts_code: "002466.SZ",
        name: "天齐锂业",
        verdict: "看多",
        confidence: 3.5,
        price_advice: { entry: 66.04, stop_loss: 60.71, target: 72.0 },
        analyzed_at: "2026-06-24T10:30:00",
        analysis_text: "MA20 支撑, 锂电板块情绪转暖",
      },
      {
        ts_code: "002050.SZ",
        name: "三花智控",
        verdict: "偏空",
        confidence: 2.0,
        price_advice: { entry: 28.0, stop_loss: 30.0, target: 24.0 },
        analyzed_at: "2026-06-20T14:00:00",
        analysis_text: "放量跌破 MA20, 短期承压",
      },
    ];
  }
  return api.get<unknown[]>("/journal");
}

/** GET /api/journal/{ts_code} — 历史 journal 列表 */
export async function getJournal(tsCode: string): Promise<unknown[]> {
  if (USE_MOCK) {
    return [
      {
        ts_code: tsCode,
        name: "天齐锂业",
        verdict: "看多",
        confidence: 3.5,
        entry_price: 66.04,
        stop_loss: 60.71,
        target: 72.0,
        analyzed_at: "2026-06-24T10:30:00",
        note: "MA20 支撑, 锂电板块情绪转暖",
      },
      {
        ts_code: tsCode,
        name: "天齐锂业",
        verdict: "偏多",
        confidence: 3.0,
        entry_price: 65.0,
        stop_loss: 60.0,
        target: 72.0,
        analyzed_at: "2026-05-12T14:15:00",
        note: "板块轮动 + 估值修复",
      },
    ];
  }
  return api.get<unknown[]>(`/journal/${encodeURIComponent(tsCode)}`);
}

/**
 * GET /api/journal/{ts_code}/latest — 最近一条 AI 分析记录。无记录返回 null。
 *
 * 返回 verdict entry(形状见 apex/analyze.py:run),其中 price_advice 含
 * entry/stop_loss/target。用于手动持仓同步 AI advice。
 */
export interface LatestJournal {
  ts_code: string;
  analyzed_at?: string;
  verdict?: string;
  calibrated_confidence?: number;
  price_advice?: {
    entry?: number | null;
    stop_loss?: number | null;
    target?: number | null;
  };
}

export async function getLatestJournal(
  tsCode: string,
): Promise<LatestJournal | null> {
  if (USE_MOCK) {
    return {
      ts_code: tsCode,
      analyzed_at: "2026-06-24T10:30:00",
      verdict: "偏多",
      calibrated_confidence: 3.5,
      price_advice: { entry: 66.04, stop_loss: 60.71, target: 72.0 },
    };
  }
  return api.get<LatestJournal | null>(
    `/journal/${encodeURIComponent(tsCode)}/latest`,
  );
}

/**
 * GET /api/trace/{ts_code}/{analyzed_at} — 某次分析的完整事件流(trace.jsonl)。
 *
 * 回放历史分析过程用。无记录返回 null(早期分析未落 trace / save=False)。
 * events 形状见 <TraceEventList> 注释(对齐真后端 apex/analyze.py:_emit)。
 * 这里用 unknown[] 而非 TraceEvent[] —— 避免 api 层反向依赖 components 层,
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
  if (USE_MOCK) {
    return null; // mock 无 trace 落盘, 回放区降级「无过程记录」
  }
  return api.get<TraceRecord | null>(
    `/trace/${encodeURIComponent(tsCode)}/${encodeURIComponent(analyzedAt)}`,
  );
}
