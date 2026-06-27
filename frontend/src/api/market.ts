import { api } from "./client";

/**
 * 行情 API
 *
 * ED9 批量请求: prices/daily 支持逗号分隔 codes 批量取, 不逐个。
 * 实时价优先, 盘中无价回退日线昨收(后端已处理)。
 *
 * PR1a 联调: DEV 模式下若未启后端, 用 mock 数据让红涨绿跌可可视化。
 * 上线后走真后端(去掉 MOCK 分支)。
 */

// dev 默认开 mock(后端大概率没跑), 设 VITE_USE_MOCK=0 走真后端
const USE_MOCK = import.meta.env.VITE_USE_MOCK !== "0";

const MOCK_DATA: Record<string, { price: number; prev: number }> = {
  "000001.SZ": { price: 11.32, prev: 11.05 }, // 平安银行 涨
  "000001.SH": { price: 3245.67, prev: 3220.34 }, // 上证 涨
  "399001.SZ": { price: 10123.45, prev: 10198.21 }, // 深证 跌
  "002466.SZ": { price: 68.50, prev: 66.04 }, // 天齐锂业 涨(对照 entry 66.04)
  "002415.SZ": { price: 32.80, prev: 32.50 }, // 海康威视 涨
  "002475.SZ": { price: 65.20, prev: 65.85 }, // 立讯精密 跌
  "603650.SH": { price: 57.30, prev: 57.80 }, // 彤程新材 跌
};

export async function getPrices(
  codes: string[],
): Promise<Record<string, number | null>> {
  if (codes.length === 0) return {};
  if (USE_MOCK) {
    const out: Record<string, number | null> = {};
    codes.forEach((c) => {
      out[c] = MOCK_DATA[c]?.price ?? null;
    });
    return out;
  }
  return api.get<Record<string, number | null>>(
    `/market/prices?codes=${codes.join(",")}`,
  );
}

export async function getDailyPrices(
  codes: string[],
): Promise<Record<string, number | null>> {
  if (codes.length === 0) return {};
  if (USE_MOCK) {
    const out: Record<string, number | null> = {};
    codes.forEach((c) => {
      out[c] = MOCK_DATA[c]?.prev ?? null;
    });
    return out;
  }
  return api.get<Record<string, number | null>>(
    `/market/prices/daily?codes=${codes.join(",")}`,
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

const MOCK_INDEX_DAILY: Record<string, IndexDaily> = {
  "000001.SH": {
    code: "000001.SH", name: "上证指数",
    bars: [
      { trade_date: "20260625", close: 4120.28, vol: 6.7e8, pct_chg: 0.23, amount: 6.5e7 },
      { trade_date: "20260626", close: 4027.26, vol: 6.6e8, pct_chg: -2.26, amount: 5.8e7 },
    ],
  },
  "399001.SZ": {
    code: "399001.SZ", name: "深证成指",
    bars: [
      { trade_date: "20260625", close: 16344.08, vol: 8.31e8, pct_chg: 1.82, amount: 7.2e7 },
      { trade_date: "20260626", close: 16000.0, vol: 8.1e8, pct_chg: -2.11, amount: 6.9e7 },
    ],
  },
  "399006.SZ": {
    code: "399006.SZ", name: "创业板指",
    bars: [
      { trade_date: "20260625", close: 2050.0, vol: 4.0e8, pct_chg: 2.1, amount: 3.5e7 },
      { trade_date: "20260626", close: 2000.0, vol: 3.9e8, pct_chg: -2.44, amount: 3.3e7 },
    ],
  },
  "000300.SH": {
    code: "000300.SH", name: "沪深300",
    bars: [
      { trade_date: "20260625", close: 4200.0, vol: 3.5e8, pct_chg: 0.8, amount: 4.0e7 },
      { trade_date: "20260626", close: 4150.0, vol: 3.4e8, pct_chg: -1.19, amount: 3.8e7 },
    ],
  },
  "000905.SH": {
    code: "000905.SH", name: "中证500",
    bars: [
      { trade_date: "20260625", close: 5500.0, vol: 2.8e8, pct_chg: 1.2, amount: 3.0e7 },
      { trade_date: "20260626", close: 5450.0, vol: 2.7e8, pct_chg: -0.91, amount: 2.9e7 },
    ],
  },
  "000688.SH": {
    code: "000688.SH", name: "科创50",
    bars: [
      { trade_date: "20260625", close: 980.0, vol: 1.2e8, pct_chg: 1.5, amount: 1.2e7 },
      { trade_date: "20260626", close: 965.0, vol: 1.1e8, pct_chg: -1.53, amount: 1.1e7 },
    ],
  },
  "399106.SZ": {
    code: "399106.SZ", name: "深证综指",
    bars: [
      { trade_date: "20260625", close: 1900.0, vol: 8.5e8, pct_chg: 1.7, amount: 7.5e7 },
      { trade_date: "20260626", close: 1860.0, vol: 8.3e8, pct_chg: -2.11, amount: 7.1e7 },
    ],
  },
};

export async function getIndexDaily(
  code: string,
  days = 2,
): Promise<IndexDaily> {
  if (USE_MOCK) {
    return MOCK_INDEX_DAILY[code] ?? { code, name: "", bars: [] };
  }
  return api.get<IndexDaily>(`/market/index-daily?code=${code}&days=${days}`);
}

export async function getIndexDailyBatch(
  codes: string[],
  days = 2,
): Promise<IndexDailyMap> {
  if (USE_MOCK) {
    const out: IndexDailyMap = {};
    codes.forEach((c) => {
      out[c] = MOCK_INDEX_DAILY[c] ?? { code: c, name: "", bars: [] };
    });
    return out;
  }
  return api.get<IndexDailyMap>(
    `/market/index-daily/batch?codes=${codes.join(",")}&days=${days}`,
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

/** GET /api/market/intraday/{ts_code}/bars — 当日分时 */
export async function getIntradayBars(tsCode: string): Promise<IntradayBars> {
  if (USE_MOCK) {
    // mock: 9:30-15:00 每分钟一根, 价格在 prev 附近随机游走
    const prev = MOCK_DATA[tsCode]?.price ?? 10;
    const bars: IntradayBar[] = [];
    let p = prev;
    const sessions = [
      ["09:30", "11:30"],
      ["13:00", "15:00"],
    ];
    for (const [s, e] of sessions) {
      const [sh, sm] = s.split(":").map(Number);
      const [eh, em] = e.split(":").map(Number);
      let m = sh * 60 + sm;
      const end = eh * 60 + em;
      for (; m < end; m++) {
        const hh = String(Math.floor(m / 60)).padStart(2, "0");
        const mm = String(m % 60).padStart(2, "0");
        const open = p;
        p = Math.max(0.01, p + (Math.sin(m) - 0.5) * 0.02);
        const close = p;
        bars.push({
          time: `2026-06-27 ${hh}:${mm}:00`,
          open,
          high: Math.max(open, close) + 0.01,
          low: Math.min(open, close) - 0.01,
          close,
          vol: 1000 + Math.abs(Math.sin(m)) * 500,
          amount: close * 1000,
        });
      }
    }
    return { trade_date: "20260627", is_intraday: true, prev_close: prev, bars };
  }
  return api.get<IntradayBars>(
    `/market/intraday/${encodeURIComponent(tsCode)}/bars`,
  );
}
