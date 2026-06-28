import { api } from "./client";

/**
 * Backtest API
 *
 * ED11(修正, outside voice #2): PR4 用**逐笔收益柱状图 + 统计表**,
 * 不画错的净值曲线(客户端 ∏(1+r) 累乘数学错)。
 *
 * /api/backtest/signals?ts_code=&lookforward_days= → 逐笔交易列表
 * /api/backtest/realized → 实际平仓分析(无 ts_code/lookforward 参数, 全量)
 *
 * 字段: ts_code / date / verdict / confidence / strategy / fill_price /
 *       net_return / benchmark_return / excess_return / max_drawdown / sharpe / hit / beat_benchmark
 */

const USE_MOCK = import.meta.env.VITE_USE_MOCK !== "0";

export interface BacktestSignal {
  ts_code: string;
  name?: string;
  date: string;
  analyzed_at?: string;
  verdict: string;
  confidence?: number;
  strategy?: string;
  fill_price?: number;
  net_return: number;
  benchmark_return?: number | null;
  excess_return?: number | null;
  max_drawdown?: number | null;
  sharpe?: number | null;
  hit: boolean;
  beat_benchmark?: boolean | null;
}

const MOCK_SIGNALS: BacktestSignal[] = [
  {
    ts_code: "002466.SZ",
    name: "天齐锂业",
    date: "2026-05-12",
    analyzed_at: "2026-05-12 10:30",
    verdict: "偏多",
    confidence: 3.5,
    strategy: "analyze",
    fill_price: 62.0,
    net_return: 0.082,
    benchmark_return: 0.012,
    excess_return: 0.07,
    max_drawdown: -0.024,
    sharpe: 1.8,
    hit: true,
    beat_benchmark: true,
  },
  {
    ts_code: "002415.SZ",
    name: "海康威视",
    date: "2026-04-15",
    analyzed_at: "2026-04-15 14:15",
    verdict: "看多",
    confidence: 4.0,
    strategy: "analyze",
    fill_price: 30.5,
    net_return: -0.035,
    benchmark_return: -0.008,
    excess_return: -0.027,
    max_drawdown: -0.061,
    sharpe: -0.5,
    hit: false,
    beat_benchmark: false,
  },
  {
    ts_code: "002475.SZ",
    name: "立讯精密",
    date: "2026-03-20",
    analyzed_at: "2026-03-20 11:00",
    verdict: "偏多",
    confidence: 3.0,
    strategy: "analyze",
    fill_price: 58.0,
    net_return: 0.124,
    benchmark_return: 0.025,
    excess_return: 0.099,
    max_drawdown: -0.018,
    sharpe: 2.1,
    hit: true,
    beat_benchmark: true,
  },
  {
    ts_code: "002466.SZ",
    name: "天齐锂业",
    date: "2026-02-28",
    analyzed_at: "2026-02-28 09:45",
    verdict: "看多",
    confidence: 3.5,
    strategy: "analyze",
    fill_price: 58.5,
    net_return: 0.051,
    benchmark_return: 0.015,
    excess_return: 0.036,
    max_drawdown: -0.031,
    sharpe: 1.2,
    hit: true,
    beat_benchmark: true,
  },
  {
    ts_code: "002415.SZ",
    name: "海康威视",
    date: "2026-01-18",
    analyzed_at: "2026-01-18 13:20",
    verdict: "偏多",
    confidence: 3.0,
    strategy: "analyze",
    fill_price: 33.2,
    net_return: -0.018,
    benchmark_return: 0.005,
    excess_return: -0.023,
    max_drawdown: -0.045,
    sharpe: -0.3,
    hit: false,
    beat_benchmark: false,
  },
];

export interface BacktestRealized {
  ts_code: string;
  name?: string;
  strategy?: string;
  regime?: string;
  entry_date: string;
  exit_date: string;
  // 兼容: 真后端 fill_price, 老 mock entry_price
  fill_price?: number;
  entry_price?: number;
  exit_price: number;
  // 兼容: 真后端 net_pnl_pct / gross_pnl_pct, 老 mock net_return
  net_pnl_pct?: number;
  net_return?: number;
  gross_pnl_pct?: number;
  exit_reason: string; // 英文 code 或中文, 由显示层映射
  // 兼容: 真后端 days_held, 老 mock hold_days
  days_held?: number;
  hold_days?: number;
  hit: boolean;
  ai_verdict?: string;
  ai_confidence?: number;
}

/** exit_reason 英文 code → 中文(用户可读) + 方向色 */
const EXIT_REASON_MAP: Record<
  string,
  { label: string; tone: "up" | "down" | "flat" }
> = {
  stop_hit: { label: "止损", tone: "down" },
  target_hit: { label: "止盈", tone: "up" },
  manual: { label: "手动", tone: "flat" },
  expired: { label: "到期", tone: "flat" },
  // 老 mock 中文(向后兼容)
  止盈: { label: "止盈", tone: "up" },
  止损: { label: "止损", tone: "down" },
};

export function resolveExitReason(
  raw: string,
): { label: string; tone: "up" | "down" | "flat" } {
  return EXIT_REASON_MAP[raw] ?? { label: raw, tone: "flat" };
}

const MOCK_REALIZED: BacktestRealized[] = [
  {
    ts_code: "603650.SH",
    name: "彤程新材",
    entry_date: "2026-04-10",
    exit_date: "2026-05-15",
    entry_price: 56.8,
    exit_price: 61.2,
    net_return: 0.0775,
    exit_reason: "止盈",
    hold_days: 35,
    hit: true,
  },
  {
    ts_code: "002466.SZ",
    name: "天齐锂业",
    entry_date: "2026-03-05",
    exit_date: "2026-04-02",
    entry_price: 60.0,
    exit_price: 58.4,
    net_return: -0.0267,
    exit_reason: "止损",
    hold_days: 28,
    hit: false,
  },
];

export async function getBacktestSignals(
  tsCode: string | null,
  lookforwardDays: number,
): Promise<BacktestSignal[]> {
  if (USE_MOCK) {
    if (!tsCode) return MOCK_SIGNALS;
    return MOCK_SIGNALS.filter((s) => s.ts_code === tsCode);
  }
  const params = new URLSearchParams();
  if (tsCode) params.set("ts_code", tsCode);
  params.set("lookforward_days", String(lookforwardDays));
  return api.get<BacktestSignal[]>(`/backtest/signals?${params}`);
}

export async function getBacktestRealized(): Promise<BacktestRealized[]> {
  if (USE_MOCK) return MOCK_REALIZED;
  return api.get<BacktestRealized[]>("/backtest/realized");
}

/* ── P2: 持有期扫描 ─────────────────────────────────────── */

export interface SweepByPeriod {
  holding_period: number;
  n: number;
  fillable_n: number;
  unfillable_count: number;
  win_rate: number | null;
  avg_net_return: number | null;
  avg_max_drawdown: number | null;
  avg_excess_return: number | null;
}

export interface SweepResult {
  per_signal: BacktestSignal[];
  by_period: SweepByPeriod[];
}

const MOCK_SWEEP: SweepResult = {
  per_signal: [],
  by_period: [
    { holding_period: 1, n: 18, fillable_n: 16, unfillable_count: 2, win_rate: 0.5625, avg_net_return: 0.0042, avg_max_drawdown: -0.015, avg_excess_return: 0.0011 },
    { holding_period: 3, n: 18, fillable_n: 16, unfillable_count: 2, win_rate: 0.5, avg_net_return: 0.0089, avg_max_drawdown: -0.028, avg_excess_return: 0.0034 },
    { holding_period: 5, n: 18, fillable_n: 16, unfillable_count: 2, win_rate: 0.5625, avg_net_return: 0.0151, avg_max_drawdown: -0.041, avg_excess_return: 0.0068 },
    { holding_period: 10, n: 18, fillable_n: 16, unfillable_count: 2, win_rate: 0.5, avg_net_return: 0.0188, avg_max_drawdown: -0.072, avg_excess_return: 0.0042 },
    { holding_period: 20, n: 18, fillable_n: 16, unfillable_count: 2, win_rate: 0.4375, avg_net_return: 0.0113, avg_max_drawdown: -0.121, avg_excess_return: -0.0021 },
  ],
};

export async function getBacktestSweep(
  tsCode: string | null,
  holdingPeriods?: number[],
): Promise<SweepResult> {
  if (USE_MOCK) return MOCK_SWEEP;
  const params = new URLSearchParams();
  if (tsCode) params.set("ts_code", tsCode);
  if (holdingPeriods && holdingPeriods.length)
    params.set("holding_periods", holdingPeriods.join(","));
  return api.get<SweepResult>(`/backtest/signals/sweep?${params}`);
}

/* ── P3: 校准切片 ─────────────────────────────────────── */

export interface AggregateBucket {
  key: string;
  n: number;
  win_rate: number | null;
  avg_net_return: number | null;
  avg_excess_return: number | null;
}

export interface AggregateResult {
  lookforward_days: number;
  total_signals: number;
  fillable_count: number;
  unfillable_count: number;
  by_confidence_bucket: AggregateBucket[];
  by_verdict: AggregateBucket[];
  by_strategy: AggregateBucket[];
}

const MOCK_AGGREGATE: AggregateResult = {
  lookforward_days: 10,
  total_signals: 42,
  fillable_count: 38,
  unfillable_count: 4,
  by_confidence_bucket: [
    { key: "1-3", n: 8, win_rate: 0.375, avg_net_return: -0.012, avg_excess_return: -0.018 },
    { key: "4-6", n: 17, win_rate: 0.529, avg_net_return: 0.008, avg_excess_return: 0.002 },
    { key: "7-10", n: 13, win_rate: 0.692, avg_net_return: 0.034, avg_excess_return: 0.027 },
  ],
  by_verdict: [
    { key: "看多", n: 21, win_rate: 0.667, avg_net_return: 0.028, avg_excess_return: 0.021 },
    { key: "偏多", n: 12, win_rate: 0.5, avg_net_return: 0.005, avg_excess_return: -0.002 },
    { key: "观望偏多", n: 5, win_rate: 0.4, avg_net_return: -0.011, avg_excess_return: -0.015 },
  ],
  by_strategy: [{ key: "standalone", n: 38, win_rate: 0.553, avg_net_return: 0.014, avg_excess_return: 0.008 }],
};

export async function getBacktestAggregate(
  tsCode: string | null,
  lookforwardDays: number,
): Promise<AggregateResult> {
  if (USE_MOCK) return MOCK_AGGREGATE;
  const params = new URLSearchParams();
  if (tsCode) params.set("ts_code", tsCode);
  params.set("lookforward_days", String(lookforwardDays));
  return api.get<AggregateResult>(`/backtest/signals/aggregate?${params}`);
}

/* ── P4: 组合级净值 ─────────────────────────────────────── */

export interface EquityPoint {
  date: string;
  equity: number;
}

export interface PortfolioTrade {
  ts_code: string;
  entry_date: string;
  exit_date: string;
  return: number;
}

export interface PortfolioStats {
  total_return: number;
  max_drawdown: number;
  sharpe: number;
  n_trades: number;
  win_rate: number;
  init_cash: number;
  final_equity: number | null;
}

export interface PortfolioResult {
  equity_curve: EquityPoint[];
  stats: Partial<PortfolioStats>;
  trades: PortfolioTrade[];
  error?: string;
}

function mkMockEquity(): EquityPoint[] {
  // 单调上升带波动的合成净值
  const pts: EquityPoint[] = [];
  let v = 1000000;
  const start = new Date("2026-01-05");
  for (let i = 0; i < 24; i++) {
    v *= 1 + (0.004 + (i % 5 === 0 ? -0.012 : 0));
    const d = new Date(start);
    d.setDate(d.getDate() + i * 7);
    pts.push({ date: d.toISOString().slice(0, 10), equity: Math.round(v) });
  }
  return pts;
}

const MOCK_PORTFOLIO: PortfolioResult = {
  equity_curve: mkMockEquity(),
  stats: {
    total_return: 0.082,
    max_drawdown: -0.038,
    sharpe: 1.42,
    n_trades: 6,
    win_rate: 0.667,
    init_cash: 1000000,
    final_equity: 1082000,
  },
  trades: [
    { ts_code: "002050.SZ", entry_date: "2026-01-08", exit_date: "2026-01-22", return: 0.034 },
    { ts_code: "600519.SH", entry_date: "2026-01-15", exit_date: "2026-01-29", return: -0.012 },
  ],
};

export async function getBacktestPortfolio(
  tsCode: string | null,
  lookforwardDays: number,
): Promise<PortfolioResult> {
  if (USE_MOCK) return MOCK_PORTFOLIO;
  const params = new URLSearchParams();
  if (tsCode) params.set("ts_code", tsCode);
  params.set("lookforward_days", String(lookforwardDays));
  return api.get<PortfolioResult>(`/backtest/portfolio?${params}`);
}

/* ── AI 回测复盘 ────────────────────────────────────────── */
/*
 * POST /api/backtest/review — 对 aggregate 切片跑 DeepSeek，产出结构化结论。
 * 字段形状以 apex/backtest_review.py:review() 的返回 dict 为准（非 mock）。
 * 阻塞调用（AI 几秒），不进 query cache，走 mutation。
 */

export type ReviewCategory =
  | "calibration"
  | "exit"
  | "strategy"
  | "regime"
  | "risk"
  | "entry";

export type ReviewSeverity = "high" | "medium" | "low";
export type WeightHint = "increase" | "decrease" | "hold";

export interface BacktestFinding {
  category: ReviewCategory;
  severity: ReviewSeverity;
  description: string;
  suggestion: string;
}

export interface BacktestReviewResult {
  summary: string;
  findings: BacktestFinding[];
  prompt_injection: string;
  strategy_weight_hint: Record<string, WeightHint>;
  strategy_stats?: {
    generated_at: string;
    lookforward_days?: number | null;
    total_signals?: number;
    by_strategy?: Record<
      string,
      {
        n: number;
        win_rate: number | null;
        avg_net_return: number | null;
        avg_excess_return: number | null;
        weight_hint: WeightHint;
      }
    >;
  } | null;
  aggregate?: AggregateResult | null;
  model?: string;
  generated_at: string;
}

export async function reviewBacktest(
  tsCode: string | null,
  lookforwardDays: number,
  model?: string,
): Promise<BacktestReviewResult> {
  // AI 调用不走 mock（mock 无意义），仅真后端可用。
  return api.post<BacktestReviewResult>("/backtest/review", {
    ts_code: tsCode ?? null,
    lookforward_days: lookforwardDays,
    model: model ?? null,
  });
}
