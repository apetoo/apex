import { api } from "./client";

/**
 * 行情 API
 *
 * ED9 批量请求: prices/daily 支持逗号分隔 codes 批量取, 不逐个。
 * 实时价优先, 盘中无价回退日线昨收(后端已处理)。
 */

export async function getPrices(
  codes: string[],
): Promise<Record<string, number | null>> {
  if (codes.length === 0) return {};
  return api.get<Record<string, number | null>>(
    `/market/prices?codes=${codes.join(",")}`,
  );
}

export async function getDailyPrices(
  codes: string[],
): Promise<Record<string, number | null>> {
  if (codes.length === 0) return {};
  return api.get<Record<string, number | null>>(
    `/market/prices/daily?codes=${codes.join(",")}`,
  );
}

/**
 * 昨收价(前一交易日收盘, 严格排除今天)。
 * /prices/daily 在盘后 tushare 发了当日 daily 后会返回今日收盘, 不能当 prev 用。
 * 「今日盈亏」的 prev 基准必须用本接口。盘中/盘后都正确。
 */
export async function getPrevClosePrices(
  codes: string[],
): Promise<Record<string, number | null>> {
  if (codes.length === 0) return {};
  return api.get<Record<string, number | null>>(
    `/market/prices/prev-close?codes=${codes.join(",")}`,
  );
}

/** 指数日线(ED13): 返回 { code, name, bars: [{ trade_date, close, vol, pct_chg, amount }] } */
export interface IndexDailyBar {
  trade_date: string;
  close: number;
  vol: number;
  pct_chg: number;
  amount?: number; // 成交额, 千元(tushare 原单位)
}
export interface IndexDaily {
  code: string;
  name: string;
  bars: IndexDailyBar[];
}
/** 批量指数日线: { [code]: IndexDaily } */
export type IndexDailyMap = Record<string, IndexDaily>;

export async function getIndexDaily(
  code: string,
  days = 2,
): Promise<IndexDaily> {
  return api.get<IndexDaily>(`/market/index-daily?code=${code}&days=${days}`);
}

export async function getIndexDailyBatch(
  codes: string[],
  days = 2,
): Promise<IndexDailyMap> {
  return api.get<IndexDailyMap>(
    `/market/index-daily/batch?codes=${codes.join(",")}&days=${days}`,
  );
}

/**
 * 指数实时行情(新浪兜底): 当 EOD 日线还没发当日数据时(盘中 / 盘后到 EOD 发布前)
 * 取当日点位 + 涨跌幅 + 成交额。amount 单位千元(后端已 ÷1000 对齐 tushare)。
 * trade_date==今天 时前端覆盖 EOD 展示; 否则回退 EOD。
 */
export interface IndexRealtime {
  code: string;
  close: number;
  prev_close: number;
  pct_chg: number | null;
  amount: number; // 千元
  trade_date: string; // YYYYMMDD
}
export type IndexRealtimeMap = Record<string, IndexRealtime>;

/** 批量指数实时(新浪): GET /api/market/index-realtime/batch?codes= */
export async function getIndexRealtimeBatch(
  codes: string[],
): Promise<IndexRealtimeMap> {
  if (codes.length === 0) return {};
  return api.get<IndexRealtimeMap>(
    `/market/index-realtime/batch?codes=${codes.join(",")}`,
  );
}

/** 当日分时 1 分钟 K 线(后端 get_intraday_bars, akshare 1min 不复权) */
export interface IntradayBar {
  time: string; // "YYYY-MM-DD HH:MM:SS" 北京时间
  open: number;
  high: number;
  low: number;
  close: number;
  vol: number;
  amount: number;
}
export interface IntradayBars {
  trade_date?: string;
  as_of_time?: string;
  is_intraday?: boolean;
  prev_close?: number | null;
  prev_vol_shou?: number | null;
  bars: IntradayBar[];
}

/** 股票基本信息(名称/行业/上市日)。失败返回 null, 调用方降级只显示代码。 */
export interface StockInfo {
  name?: string;
  industry?: string;
  list_date?: string;
  market?: string;
}

export async function getStockInfo(tsCode: string): Promise<StockInfo | null> {
  try {
    // 后端 data.get_stock_info 走 df.to_json(orient="records") -> 是 **records 数组**,
    // 即便只一行也是 [{...}]。错误态才是 {"error":...} 单对象。
    const res = await api.get<StockInfo | StockInfo[] | { error: string }>(
      `/market/stocks/${encodeURIComponent(tsCode)}/info`,
    );
    if (!res) return null;
    if (Array.isArray(res)) return res[0] ?? null;
    if ("error" in res) return null;
    return res;
  } catch {
    return null;
  }
}

/** GET /api/market/intraday/{ts_code}/bars - 当日分时 */
export async function getIntradayBars(
  tsCode: string,
  tradeDate?: string,
): Promise<IntradayBars> {
  const qs = tradeDate ? `?trade_date=${tradeDate}` : "";
  return api.get<IntradayBars>(
    `/market/intraday/${encodeURIComponent(tsCode)}/bars${qs}`,
  );
}
