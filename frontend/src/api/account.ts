/**
 * Account API
 *
 * GET /api/account — 账户配置(总资金/风险参数)
 * GET /api/account/risk — 当前持仓总风险
 * GET /api/account/summary — 总资产汇总(本金+已实现+浮盈)
 */

import { api } from "./client";

const USE_MOCK = import.meta.env.VITE_USE_MOCK !== "0";

export interface AccountData {
  total_capital: number;
  risk_per_trade_pct: number;
  max_total_risk_pct: number;
}

export interface AccountRisk {
  total_risk_pct: number;
  total_risk_amount: number;
  over_limit: boolean;
  positions: Array<{
    ts_code: string;
    name: string;
    risk_amount: number;
    risk_pct: number;
  }>;
}

export interface AccountSummary {
  total_capital: number;
  market_value: number;
  cost_basis: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number | null;
  realized_pnl_total: number;
  total_assets: number;
  total_return_pct: number | null;
  position_count: number;
  missing_price_count: number;
  as_of: string;
}

const MOCK_ACCOUNT: AccountData = {
  total_capital: 100000,
  risk_per_trade_pct: 1.0,
  max_total_risk_pct: 10.0,
};

const MOCK_RISK: AccountRisk = {
  total_risk_pct: 0.53,
  total_risk_amount: 533,
  over_limit: false,
  positions: [
    { ts_code: "002466.SZ", name: "天齐锂业", risk_amount: 533, risk_pct: 0.53 },
  ],
};

const MOCK_SUMMARY: AccountSummary = {
  total_capital: 100000,
  market_value: 53300,
  cost_basis: 52000,
  unrealized_pnl: 1300,
  unrealized_pnl_pct: 2.5,
  realized_pnl_total: 3200,
  total_assets: 104500,
  total_return_pct: 4.5,
  position_count: 3,
  missing_price_count: 0,
  as_of: "2026-07-05T01:00:00+08:00",
};

/** GET /api/account */
export async function getAccount(): Promise<AccountData> {
  if (USE_MOCK) return MOCK_ACCOUNT;
  return api.get<AccountData>("/account");
}

/** GET /api/account/risk */
export async function getAccountRisk(): Promise<AccountRisk> {
  if (USE_MOCK) return MOCK_RISK;
  return api.get<AccountRisk>("/account/risk");
}

/** GET /api/account/summary */
export async function getAccountSummary(): Promise<AccountSummary> {
  if (USE_MOCK) return MOCK_SUMMARY;
  return api.get<AccountSummary>("/account/summary");
}
