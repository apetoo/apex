/**
 * Account API
 *
 * GET /api/account - 账户配置(总资金/风险参数)
 * GET /api/account/risk - 当前持仓总风险
 * GET /api/account/summary - 总资产汇总(本金+已实现+浮盈)
 */

import { api } from "./client";

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

/** GET /api/account */
export async function getAccount(): Promise<AccountData> {
  return api.get<AccountData>("/account");
}

/** GET /api/account/risk */
export async function getAccountRisk(): Promise<AccountRisk> {
  return api.get<AccountRisk>("/account/risk");
}

/** GET /api/account/summary */
export async function getAccountSummary(): Promise<AccountSummary> {
  return api.get<AccountSummary>("/account/summary");
}
