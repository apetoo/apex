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
 * 把真后端响应适配成前端 ScreenerReport 形状。两条路径共用:
 *   - REST  /api/screener/report  (落盘报告)
 *   - SSE   /api/screener/run done 事件 (sc.run() 返回值)
 *
 * 落盘报告 (apex/screener.py:_write_log):
 *   { trade_date, generated_at, regime, strategy_weights, weights_source,
 *     selector_reasoning, by_strategy: { <name>: { n_candidates, weight, top_5 } },
 *     top_scored: [...], all_candidates_summary, stats: { total_candidates, top_n } }
 *
 * sc.run() 返回值 (SSE done 直接透传, 无 stats):
 *   { trade_date, out_path, top_scored, by_strategy: { <name>: <count> },
 *     strategy_weights, weights_source, selector_reasoning, regime, total_candidates }
 *
 * 前端期望: { date, picks, summary, ai_summary?, regime?, stats? }
 */
export function adaptScreenerReport(
  raw: Record<string, unknown>,
): ScreenerReport {
  const topScored = (raw.top_scored as Array<Record<string, unknown>>) ?? [];
  const byStrategyRaw =
    (raw.by_strategy as Record<string, unknown>) ?? {};
  const stats =
    (raw.stats as { total_candidates?: number; top_n?: number }) ?? {};

  // by_strategy 两种形状都接: run() 是 { name: number }, 落盘是 { name: { n_candidates, ... } }
  const countOf = (v: unknown): number =>
    typeof v === "number"
      ? v
      : ((v as { n_candidates?: number })?.n_candidates ?? 0);

  const byStrategyCounts: Record<string, number> = {};
  for (const [k, v] of Object.entries(byStrategyRaw)) {
    byStrategyCounts[k] = countOf(v);
  }

  const totalCandidates =
    stats.total_candidates ??
    (raw.total_candidates as number | undefined) ??
    Object.values(byStrategyCounts).reduce((a, b) => a + b, 0);

  // picks: 把 top_scored 拍平, 通过率用 strategy 命中数 / 5(每策略 top_5)
  const picks = topScored.map((p) => {
    const strategy = p.strategy as string | undefined;
    const n = strategy ? byStrategyCounts[strategy] ?? 0 : 0;
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
    stats: {
      total_candidates: stats.total_candidates ?? totalCandidates,
      top_n: stats.top_n ?? topScored.length,
    },
  };
}

export async function getAvailableDates(): Promise<string[]> {
  if (USE_MOCK) return ["2026-06-23", "2026-06-24", "2026-06-25"];
  return api.get<string[]>("/screener/dates");
}

/**
 * 因子评测表（策略体检）：IC + pool-alpha + 4 态 verdict。
 *
 * GET /screener/factor-ic —— 读已落盘的 factor_ic.json（快，不跑回测），无则 null。
 * POST /screener/factor-ic/run —— 触发一次回测（同步，涨停池基准开启时可能数分钟）。
 *
 * verdict 4 态优先级: n_insufficient > alpha_unavailable > dead_weight > live_candidate
 * 字段形状以 apex/screener_backtest.py:run 返回的 factor_ic payload 为准（别按 mock 抄）。
 */
export interface FactorIcRow {
  key: string;
  n: number;
  fillable_n: number;
  unfillable_count: number;
  benchmark: string;
  benchmark_n: number;
  benchmark_missing_count: number;
  ic_1d: number | null;
  ic_5d: number | null;
  ic_10d: number | null;
  ir_5d: number | null;
  t_stat_5d: number | null;
  n_dates_5d: number;
  pool_alpha_5d: number | null;
  verdict: "n_insufficient" | "alpha_unavailable" | "dead_weight" | "live_candidate";
}

export interface FactorIcPayload {
  generated_at: string;
  lookforward_days: number;
  horizons: number[];
  reports_scanned: number;
  reports_used: number;
  total_candidates: number;
  thresholds: {
    min_fillable_n: number;
    min_n_dates: number;
    min_benchmark_n: number;
    ic_dead: number;
    alpha_dead: number;
  };
  by_strategy: FactorIcRow[];
}

export async function getFactorIc(): Promise<FactorIcPayload | null> {
  if (USE_MOCK) return MOCK_FACTOR_IC;
  return api.get<FactorIcPayload | null>("/screener/factor-ic");
}

export async function runFactorIc(noBenchmark = true): Promise<FactorIcPayload> {
  // 真后端同步触发；mock 直接返回 mock 数据（dev 下不真跑回测）
  if (USE_MOCK) return MOCK_FACTOR_IC;
  return api.post<FactorIcPayload>(
    `/screener/factor-ic/run?no_benchmark=${noBenchmark ? "true" : "false"}`,
  );
}

const MOCK_FACTOR_IC: FactorIcPayload = {
  generated_at: "2026-06-30T13:00:00+08:00",
  lookforward_days: 10,
  horizons: [1, 5, 10],
  reports_scanned: 11,
  reports_used: 8,
  total_candidates: 110,
  thresholds: {
    min_fillable_n: 10,
    min_n_dates: 8,
    min_benchmark_n: 5,
    ic_dead: 0.05,
    alpha_dead: 0.01,
  },
  by_strategy: [
    {
      key: "leader_with_volume",
      n: 16, fillable_n: 14, unfillable_count: 2,
      benchmark: "limit_up_pool_skipped", benchmark_n: 0, benchmark_missing_count: 14,
      ic_1d: 0.88, ic_5d: 0.27, ic_10d: 0.21,
      ir_5d: null, t_stat_5d: null, n_dates_5d: 1,
      pool_alpha_5d: null, verdict: "n_insufficient",
    },
    {
      key: "first_board_leader",
      n: 38, fillable_n: 37, unfillable_count: 1,
      benchmark: "limit_up_pool_skipped", benchmark_n: 0, benchmark_missing_count: 37,
      ic_1d: 0.21, ic_5d: -0.12, ic_10d: 0.15,
      ir_5d: -0.5, t_stat_5d: -0.7, n_dates_5d: 2,
      pool_alpha_5d: null, verdict: "n_insufficient",
    },
    {
      key: "industry_rotation",
      n: 15, fillable_n: 15, unfillable_count: 0,
      benchmark: "industry_sector_unavailable", benchmark_n: 0, benchmark_missing_count: 15,
      ic_1d: -0.37, ic_5d: 0.03, ic_10d: -0.03,
      ir_5d: null, t_stat_5d: null, n_dates_5d: 1,
      pool_alpha_5d: null, verdict: "n_insufficient",
    },
  ],
};


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
