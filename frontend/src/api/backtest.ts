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
  entry_date: string;
  exit_date: string;
  entry_price: number;
  exit_price: number;
  net_return: number;
  exit_reason: string;
  hold_days: number;
  hit: boolean;
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
