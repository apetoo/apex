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
 */

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

export async function getStrategies(): Promise<ScreenerStrategy[]> {
  const data = await api.get<{
    weights: ScreenerWeights;
    strategies: ScreenerStrategy[];
  }>("/screener/weights");
  return data.strategies;
}

export async function getDefaultWeights(): Promise<ScreenerWeights> {
  const data = await api.get<{
    weights: ScreenerWeights;
    strategies: ScreenerStrategy[];
  }>("/screener/weights");
  return data.weights;
}

export async function getReport(date?: string): Promise<ScreenerReport | null> {
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
  return api.get<string[]>("/screener/dates");
}

/**
 * 因子评测表（策略体检）：IC + pool-alpha + 4 态 verdict。
 *
 * GET /screener/factor-ic -- 读已落盘的 factor_ic.json（快，不跑回测），无则 null。
 * POST /screener/factor-ic/run -- 触发一次回测（同步，涨停池基准开启时可能数分钟）。
 *
 * verdict 4 态优先级: n_insufficient > alpha_unavailable > dead_weight > live_candidate
 * 字段形状以 apex/screener_backtest.py:run 返回的 factor_ic payload 为准。
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
  return api.get<FactorIcPayload | null>("/screener/factor-ic");
}

export async function runFactorIc(noBenchmark = true): Promise<FactorIcPayload> {
  return api.post<FactorIcPayload>(
    `/screener/factor-ic/run?no_benchmark=${noBenchmark ? "true" : "false"}`,
  );
}

export function screenerFetchFn(weights: ScreenerWeights) {
  return async (params: {
    path: string;
    method: string;
    body: unknown;
    signal: AbortSignal;
  }): Promise<Response> => {
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
    // localStorage 不可用(隐私模式/配额满)-> 静默, 用内存默认
  }
}
