import { api } from "./client";

/**
 * Screener API
 *
 * SSE 事件:
 *   trace(trace.data.type === "progress" 是进度, 后端用 progress_wrapper 包了)
 *   done(最终报告)
 *   error
 *
 * ED16(修正, outside voice #12): 权重存 localStorage, 不持久化到后端
 * ED8(修正, outside voice #7): SSE 单测不依赖 MSW, 同 analyze/chat
 */

const USE_MOCK = import.meta.env.VITE_USE_MOCK !== "0";

export interface ScreenerStrategy {
  name: string;
  description: string;
}

export interface ScreenerWeights {
  [strategyName: string]: number;
}

export interface ScreenerReport {
  date: string;
  picks: Array<{
    ts_code: string;
    name: string;
    score: number;
    passed_filters: number;
    total_filters: number;
    notes?: string;
  }>;
  summary: {
    total_screened: number;
    passed: number;
    by_strategy: Record<string, number>;
  };
  ai_summary?: string;
}

const MOCK_STRATEGIES: ScreenerStrategy[] = [
  { name: "momentum", description: "动量: 突破 MA20 + 量比 > 1.5" },
  { name: "value", description: "价值: PE-TTM < 行业均值 + PB < 2" },
  { name: "growth", description: "成长: 营收同比 > 20% + 净利润同比 > 15%" },
  { name: "reversal", description: "反转: RSI < 30 + 远离 MA20" },
];

const MOCK_REPORT: ScreenerReport = {
  date: "2026-06-25",
  picks: [
    {
      ts_code: "002466.SZ",
      name: "天齐锂业",
      score: 0.85,
      passed_filters: 4,
      total_filters: 5,
      notes: "锂电板块情绪修复, 量能温和放大",
    },
    {
      ts_code: "002415.SZ",
      name: "海康威视",
      score: 0.78,
      passed_filters: 3,
      total_filters: 5,
      notes: "估值合理, 智能物联业务有韧性",
    },
    {
      ts_code: "002475.SZ",
      name: "立讯精密",
      score: 0.72,
      passed_filters: 3,
      total_filters: 5,
      notes: "果链龙头, 关注新机周期",
    },
  ],
  summary: {
    total_screened: 5423,
    passed: 87,
    by_strategy: { momentum: 32, value: 28, growth: 18, reversal: 9 },
  },
  ai_summary:
    "今日情绪面回暖, 锂电板块资金关注度提升; 价值股整体偏弱; 反转信号在科创板小票中较多, 需谨慎追高。",
};

export async function getStrategies(): Promise<ScreenerStrategy[]> {
  if (USE_MOCK) return MOCK_STRATEGIES;
  const data = await api.get<{
    weights: ScreenerWeights;
    strategies: ScreenerStrategy[];
  }>("/screener/weights");
  return data.strategies;
}

export async function getDefaultWeights(): Promise<ScreenerWeights> {
  if (USE_MOCK) {
    return { momentum: 0.25, value: 0.25, growth: 0.25, reversal: 0.25 };
  }
  const data = await api.get<{
    weights: ScreenerWeights;
    strategies: ScreenerStrategy[];
  }>("/screener/weights");
  return data.weights;
}

export async function getReport(date?: string): Promise<ScreenerReport | null> {
  if (USE_MOCK) return MOCK_REPORT;
  return api.get<ScreenerReport | null>(
    date ? `/screener/report?date=${date}` : "/screener/report",
  );
}

export async function getAvailableDates(): Promise<string[]> {
  if (USE_MOCK) return ["2026-06-23", "2026-06-24", "2026-06-25"];
  return api.get<string[]>("/screener/dates");
}

function sseEncode(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** dev mock: 流式进度 + 终态报告 */
export function buildMockScreenerFetcher() {
  return async (): Promise<Response> => {
    const encoder = new TextEncoder();
    const progressSteps = [
      "加载股票池(沪深京 A 股 5423 只)...",
      "执行动量策略...",
      "执行价值策略...",
      "执行成长策略...",
      "执行反转策略...",
      "AI 综合评估中...",
    ];
    const stream = new ReadableStream<Uint8Array>({
      async start(controller) {
        for (const msg of progressSteps) {
          await new Promise((r) => setTimeout(r, 300));
          controller.enqueue(
            encoder.encode(
              sseEncode("trace", { type: "progress", message: msg }),
            ),
          );
        }
        await new Promise((r) => setTimeout(r, 200));
        controller.enqueue(encoder.encode(sseEncode("done", MOCK_REPORT)));
        controller.close();
      },
    });
    return new Response(stream, { status: 200 }) as unknown as Response;
  };
}

export function screenerFetchFn(weights: ScreenerWeights) {
  return async (params: {
    path: string;
    method: string;
    body: unknown;
    signal: AbortSignal;
  }): Promise<Response> => {
    if (USE_MOCK) {
      // 真后端路径才用 weights, mock 忽略(让函数签名仍可带 weights 准备切真后端)
      void weights;
      return buildMockScreenerFetcher()();
    }
    return fetch(params.path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        strategy_weights: weights,
        skip_ai: false,
        skip_selector: false,
      }),
      signal: params.signal,
    });
  };
}

/** ED16: 权重持久化到 localStorage */
const WEIGHTS_KEY = "apex.screener.weights";

export function loadStoredWeights(
  defaultWeights: ScreenerWeights,
): ScreenerWeights {
  if (typeof window === "undefined") return defaultWeights;
  try {
    const raw = localStorage.getItem(WEIGHTS_KEY);
    if (!raw) return defaultWeights;
    const parsed = JSON.parse(raw) as ScreenerWeights;
    return { ...defaultWeights, ...parsed };
  } catch {
    return defaultWeights;
  }
}

export function saveWeights(weights: ScreenerWeights): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(WEIGHTS_KEY, JSON.stringify(weights));
  } catch {
    // localStorage 不可用(隐私模式/配额满)→ 静默, 用内存默认
  }
}
