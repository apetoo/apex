import { api } from "./client";

/**
 * Backtest API
 *
 * ED11(修正, outside voice #2): PR4 用**逐笔收益柱状图 + 统计表**,
 * 不画错的净值曲线(客户端 ∏(1+r) 累乘数学错)。
 *
 * /api/backtest/signals?ts_code=&lookforward_days= -> 逐笔交易列表
 * /api/backtest/realized -> 实际平仓分析(无 ts_code/lookforward 参数, 全量)
 *
 * 字段: ts_code / date / verdict / confidence / strategy / fill_price /
 *       net_return / benchmark_return / excess_return / max_drawdown / sharpe / hit / beat_benchmark
 */

export interface BacktestSignal {
  ts_code: string;
  name?: string;
  date: string;
  analyzed_at?: string;
  verdict: string;
  opinion_verdict?: string;
  trade_action?: "buy" | "watch" | "avoid";
  gate_reasons?: string[];
  confidence?: number;
  strategy?: string;
  fill_price?: number;
  status:
    | "completed"
    | "pending"
    | "pending_entry"
    | "expired_unfilled"
    | "data_truncated"
    | "unfillable"
    | "no_fill_data";
  net_return: number | null;
  benchmark_return?: number | null;
  excess_return?: number | null;
  max_drawdown?: number | null;
  sharpe?: number | null;
  hit: boolean | null;
  exit_date?: string | null;
  exit_price?: number | null;
  invalid_price_advice?: boolean;
  beat_benchmark?: boolean | null;
}

export interface BacktestRealized {
  ts_code: string;
  name?: string;
  strategy?: string;
  regime?: string;
  entry_date: string;
  exit_date: string;
  fill_price?: number;
  entry_price?: number;
  exit_price: number;
  net_pnl_pct?: number;
  net_return?: number;
  gross_pnl_pct?: number;
  exit_reason: string;
  days_held?: number;
  hold_days?: number;
  hit: boolean;
  ai_verdict?: string;
  ai_confidence?: number;
}

/** exit_reason 英文 code -> 中文(用户可读) + 方向色 */
const EXIT_REASON_MAP: Record<
  string,
  { label: string; tone: "up" | "down" | "flat" }
> = {
  stop_hit: { label: "止损", tone: "down" },
  target_hit: { label: "止盈", tone: "up" },
  manual: { label: "手动", tone: "flat" },
  expired: { label: "到期", tone: "flat" },
  止盈: { label: "止盈", tone: "up" },
  止损: { label: "止损", tone: "down" },
};

export function resolveExitReason(
  raw: string,
): { label: string; tone: "up" | "down" | "flat" } {
  return EXIT_REASON_MAP[raw] ?? { label: raw, tone: "flat" };
}

export async function getBacktestSignals(
  tsCode: string | null,
  lookforwardDays: number,
): Promise<BacktestSignal[]> {
  const params = new URLSearchParams();
  if (tsCode) params.set("ts_code", tsCode);
  params.set("lookforward_days", String(lookforwardDays));
  return api.get<BacktestSignal[]>(`/backtest/signals?${params}`);
}

export async function getBacktestRealized(): Promise<BacktestRealized[]> {
  return api.get<BacktestRealized[]>("/backtest/realized");
}

/* ── P2: 持有期扫描 ─────────────────────────────────────── */

export interface SweepByPeriod {
  holding_period: number;
  n: number;
  fillable_n: number;
  completed_count: number;
  pending_count: number;
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

export async function getBacktestSweep(
  tsCode: string | null,
  holdingPeriods?: number[],
): Promise<SweepResult> {
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
  completed_count: number;
  pending_count: number;
  unfillable_count: number;
  truncated_count: number;
  no_fill_data_count: number;
  by_confidence_bucket: AggregateBucket[];
  by_verdict: AggregateBucket[];
  by_strategy: AggregateBucket[];
}

export async function getBacktestAggregate(
  tsCode: string | null,
  lookforwardDays: number,
): Promise<AggregateResult> {
  const params = new URLSearchParams();
  if (tsCode) params.set("ts_code", tsCode);
  params.set("lookforward_days", String(lookforwardDays));
  return api.get<AggregateResult>(`/backtest/signals/aggregate?${params}`);
}

/* ── 可执行信号影子对照 ─────────────────────────────────── */

export interface ShadowArm {
  analyzed_count: number;
  gate_passed_count: number;
  gate_pass_rate: number | null;
  filled_count: number;
  completed_count: number;
  win_rate: number | null;
  avg_net_return: number | null;
  profit_factor: number | null;
  worst_max_drawdown: number | null;
  max_drawdown?: number | null;
  by_regime?: Record<string, {
    completed_count: number;
    wins: number;
    win_rate: number | null;
    avg_net_return: number | null;
  }>;
  pending_count: number;
  pending_entry_count: number;
  expired_unfilled_count: number;
  unfillable_count: number;
  no_fill_data_count: number;
  first_30_win_rate: number | null;
  last_30_win_rate: number | null;
}

export interface ShadowResult {
  gate_version: string;
  mode: "off" | "shadow" | "enforced";
  analyzed_count: number;
  arms: {
    baseline: ShadowArm;
    execution_only: ShadowArm;
    challenger_v1: ShadowArm;
  };
}

export async function getBacktestShadow(tsCode: string | null): Promise<ShadowResult> {
  const params = new URLSearchParams();
  if (tsCode) params.set("ts_code", tsCode);
  return api.get<ShadowResult>(`/backtest/shadow?${params}`);
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
  win_rate: number | null;
  open_positions: number;
  init_cash: number;
  final_equity: number | null;
}

export interface PortfolioResult {
  equity_curve: EquityPoint[];
  stats: Partial<PortfolioStats>;
  trades: PortfolioTrade[];
  error?: string;
}

export async function getBacktestPortfolio(
  tsCode: string | null,
  lookforwardDays: number,
): Promise<PortfolioResult> {
  const params = new URLSearchParams();
  if (tsCode) params.set("ts_code", tsCode);
  params.set("lookforward_days", String(lookforwardDays));
  return api.get<PortfolioResult>(`/backtest/portfolio?${params}`);
}

/* ── AI 回测复盘 ────────────────────────────────────────── */
/*
 * POST /api/backtest/review - 对 aggregate 切片跑 DeepSeek，产出结构化结论。
 * 字段形状以 apex/backtest_review.py:review() 的返回 dict 为准。
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
  return api.post<BacktestReviewResult>("/backtest/review", {
    ts_code: tsCode ?? null,
    lookforward_days: lookforwardDays,
    model: model ?? null,
  });
}
