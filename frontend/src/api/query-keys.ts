/**
 * react-query query keys 集中定义
 *
 * ED3 失效矩阵的失效目标就是这些 key。
 * 写操作 onSuccess 照表 invalidate, 不漏。
 */
export const qk = {
  watchlist: ["watchlist"] as const,
  account: ["account"] as const,
  triggers: ["triggers"] as const,
  closed: ["closed"] as const,
  // 近 N 日已平仓（概览今日盈亏用）；close/sell mutation 失效 ["closed"] 前缀即覆盖。
  closedRecent: ["closed", "recent"] as const,
  trades: ["trades"] as const,
  calibration: ["calibration"] as const,
  // 带参数的细粒度 key
  prices: (codes: string[]) => ["market", "prices", codes] as const,
  dailyPrices: (codes: string[]) => ["market", "prices-daily", codes] as const,
  prevClose: (codes: string[]) => ["market", "prices-prev-close", codes] as const,
  stockDaily: (tsCode: string) => ["market", "stock", tsCode, "daily"] as const,
  intraday: (tsCode: string) => ["market", "intraday", tsCode] as const,
  indexDailyBatch: (codes: string[]) => ["market", "index-daily", codes] as const,
  indexRealtimeBatch: (codes: string[]) => ["market", "index-realtime", codes] as const,
  journal: (tsCode: string) => ["journal", tsCode] as const,
  journalLatest: (tsCode: string) => ["journal", tsCode, "latest"] as const,
  journalAll: ["journal", "all"] as const,
  trace: (tsCode: string, analyzedAt: string) => ["trace", tsCode, analyzedAt] as const,
} as const;
