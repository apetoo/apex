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
    // 真后端 top_scored 富字段(可选, 详情页可展示)
    strategy?: string;
    strategy_reasoning?: string;
    one_liner?: string;
    ai_score?: number;
    verdict?: string;
    actionable?: string;
    red_flag?: boolean;
  }>;
  summary: {
    total_screened: number;
    passed: number;
    by_strategy: Record<string, number>;
  };
  ai_summary?: string;
  // 真后端独有字段
  regime?: {
    label: string;
    summary: string;
  };
  stats?: {
    total_candidates: number;
    top_n: number;
  };
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
  const raw = await api.get<Record<string, unknown> | null>(
    date ? `/screener/report?date=${date}` : "/screener/report",
  );
  if (!raw) return null;
  return adaptScreenerReport(raw);
}

/**
 * 把真后端 /api/screener/report 响应适配成前端 ScreenerReport 形状。
 *
 * 真后端返回(backend/routers/screener.py + apex/screener.py):
 *   { trade_date, generated_at, regime, strategy_weights, weights_source,
 *     selector_reasoning, by_strategy: { <name>: { n_candidates, weight, top_5 } },
 *     top_scored: [...], all_candidates_summary, stats: { total_candidates, top_n } }
 *
 * 前端期望: { date, picks, summary, ai_summary?, regime?, stats? }
 */
function adaptScreenerReport(
  raw: Record<string, unknown>,
): ScreenerReport {
  const topScored = (raw.top_scored as Array<Record<string, unknown>>) ?? [];
  const byStrategy =
    (raw.by_strategy as Record<string, Record<string, unknown>>) ?? {};
  const stats = (raw.stats as { total_candidates?: number; top_n?: number }) ?? {};

  // picks: 把 top_scored 拍平, 通过率用 strategy 命中数 / 5(每策略 top_5)
  const picks = topScored.map((p) => {
    const strategy = p.strategy as string | undefined;
    const strat = strategy ? byStrategy[strategy] : undefined;
    const n = (strat?.n_candidates as number) ?? 0;
    return {
      ts_code: p.ts_code as string,
      name: (p.name as string) ?? (p.ts_code as string),
      score: (p._weighted_score as number) ?? (p.strategy_score as number) ?? 0,
      passed_filters: n > 0 ? Math.min(5, Math.ceil((p.ai_score as number) ?? 3)) : 1,
      total_filters: 5,
      notes:
        (p.one_liner as string) ??
        (p.strategy_reasoning as string) ??
        (strategy ?? ""),
      strategy,
      strategy_reasoning: p.strategy_reasoning as string | undefined,
      one_liner: p.one_liner as string | undefined,
      ai_score: p.ai_score as number | undefined,
      verdict: p.verdict as string | undefined,
      actionable: p.actionable as string | undefined,
      red_flag: p.red_flag as boolean | undefined,
    };
  });

  // summary: total_screened 没法精确(后端不返回)用 stats.total_candidates 兜底
  // passed = top_scored.length, by_strategy = n_candidates 倒填
  const byStrategyCounts: Record<string, number> = {};
  for (const [k, v] of Object.entries(byStrategy)) {
    byStrategyCounts[k] = (v.n_candidates as number) ?? 0;
  }
  const totalCandidates =
    stats.total_candidates ??
    Object.values(byStrategy).reduce(
      (a, v) => a + ((v.n_candidates as number) ?? 0),
      0,
    );

  return {
    date: (raw.trade_date as string) ?? "",
    picks,
    summary: {
      total_screened: totalCandidates,
      passed: topScored.length,
      by_strategy: byStrategyCounts,
    },
    // selector_reasoning 是后端给 AI 选的"为什么这样配权重", 不是 AI summary,
    // 但前端没专门的字段就先塞进 ai_summary 让用户看到
    ai_summary: (raw.selector_reasoning as string) ?? undefined,
    regime: raw.regime as { label: string; summary: string } | undefined,
    stats,
  };
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
