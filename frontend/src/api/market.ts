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

/** 指数日线(ED13): 返回 { code, name, bars: [{ trade_date, close, vol, pct_chg }] } */
export interface IndexDailyBar {
  trade_date: string;
  close: number;
  vol: number;
  pct_chg: number;
}
export interface IndexDaily {
  code: string;
  name: string;
  bars: IndexDailyBar[];
}

const MOCK_INDEX_DAILY: Record<string, IndexDaily> = {
  "000001.SH": {
    code: "000001.SH",
    name: "上证指数",
    bars: [
      { trade_date: "20260624", close: 4110.81, vol: 644527518, pct_chg: 0.11 },
      { trade_date: "20260625", close: 4120.28, vol: 670459917, pct_chg: 0.23 },
    ],
  },
  "399001.SZ": {
    code: "399001.SZ",
    name: "深证成指",
    bars: [
      { trade_date: "20260624", close: 16051.32, vol: 790129563, pct_chg: 1.24 },
      { trade_date: "20260625", close: 16344.08, vol: 830525212, pct_chg: 1.82 },
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
