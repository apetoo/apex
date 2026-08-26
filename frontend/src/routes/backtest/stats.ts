import type { AggregateResult, BacktestSignal } from "@/api/backtest";

export interface BacktestStats {
  count: number;
  pendingCount: number;
  unfillableCount: number;
  winRate: number | null;
  avgNet: number | null;
  avgExcess: number | null;
  avgSharpe: number | null;
}

export function computeStats(
  data: Array<{
    status: BacktestSignal["status"];
    net_return: number | null;
    excess_return?: number | null;
    sharpe?: number | null;
    hit: boolean | null;
  }>,
): BacktestStats {
  const official = data.filter(
    (item): item is typeof item & { net_return: number; hit: boolean } =>
      (item.status === "completed" || item.status === "data_truncated") &&
      item.net_return != null && item.hit != null,
  );
  const pendingCount = data.filter(
    (item) => item.status === "pending" || item.status === "pending_entry",
  ).length;
  const unfillableCount = data.filter(
    (item) => item.status === "unfillable" || item.status === "no_fill_data",
  ).length;
  if (official.length === 0) {
    return {
      count: 0,
      pendingCount,
      unfillableCount,
      winRate: null,
      avgNet: null,
      avgExcess: null,
      avgSharpe: null,
    };
  }
  const average = (values: number[]) =>
    values.reduce((sum, value) => sum + value, 0) / values.length;
  const excesses = official
    .map((item) => item.excess_return)
    .filter((value): value is number => value != null);
  const sharpes = official
    .map((item) => item.sharpe)
    .filter((value): value is number => value != null);
  return {
    count: official.length,
    pendingCount,
    unfillableCount,
    winRate: official.filter((item) => item.hit).length / official.length,
    avgNet: average(official.map((item) => item.net_return)),
    avgExcess: excesses.length > 0 ? average(excesses) : null,
    avgSharpe: sharpes.length > 0 ? average(sharpes) : null,
  };
}

export function getSignalOutcome(
  signal: Pick<BacktestSignal, "status" | "hit">,
): { label: string; tone: "up" | "down" | "flat" } {
  if (signal.status === "pending") return { label: "进行中", tone: "flat" };
  if (signal.status === "pending_entry") return { label: "等待入场", tone: "flat" };
  if (signal.status === "expired_unfilled") return { label: "入场未触发", tone: "flat" };
  if (signal.status === "unfillable") return { label: "不可成交", tone: "flat" };
  if (signal.status === "no_fill_data") return { label: "无行情", tone: "flat" };
  return signal.hit
    ? { label: "盈利", tone: "up" }
    : { label: "亏损", tone: "down" };
}

const GATE_REASON_LABELS: Record<string, string> = {
  reward_risk_too_low: "盈亏比不足",
  entry_band_invalid: "入场区间无效",
  invalid_price_advice: "价格建议无效",
  evidence_insufficient: "证据不足",
  confidence_too_low: "置信度不足",
};

export function getTradeDecisionText(
  signal: Pick<BacktestSignal, "trade_action" | "gate_reasons">,
): { label: string; reasons: string } | null {
  if (!signal.trade_action) return null;
  const labels = { buy: "买入", watch: "观察", avoid: "回避" } as const;
  return {
    label: labels[signal.trade_action],
    reasons: (signal.gate_reasons ?? [])
      .map((reason) => GATE_REASON_LABELS[reason] ?? reason)
      .join("、"),
  };
}

export function hasAggregateSignals(
  aggregate: AggregateResult | null | undefined,
): aggregate is AggregateResult;
export function hasAggregateSignals(
  aggregate: { total_signals: number } | null | undefined,
): boolean;
export function hasAggregateSignals(
  aggregate: { total_signals: number } | null | undefined,
): boolean {
  return aggregate != null && aggregate.total_signals > 0;
}
