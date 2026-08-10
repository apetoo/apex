import { api } from "./client";

/**
 * 缠论结构 API（/chan 页）
 *
 * 后端 backend/routers/chan.py -> apex.chan.get_structure。
 * 口径披露见后端 docstring：买卖点为"本级别近似未经次级别确认"，
 * 最后一笔未完成会随行情重画（repaint 是缠论固有属性）。
 */

export type ChanFreq = "D" | "W" | "30" | "60";

export const CHAN_BSP_LABEL: Record<string, string> = {
  "1buy": "一买",
  "2buy": "二买",
  "3buy": "三买",
  "1sell": "一卖",
  "2sell": "二卖",
  "3sell": "三卖",
};

export interface ChanBar {
  dt: string; // D/W: "YYYY-MM-DD"；30/60: "YYYY-MM-DD HH:MM"
  open: number;
  high: number;
  low: number;
  close: number;
  vol: number;
}

export interface ChanBi {
  sdt: string;
  edt: string;
  direction: "up" | "down";
  high: number;
  low: number;
  confirmed: boolean;
}

export interface ChanZs {
  sdt: string;
  edt: string;
  zg: number;
  zd: number;
  zz: number;
  state: "confirmed" | "extending";
}

export interface ChanBsp {
  dt: string;
  type: "1buy" | "1sell" | "2buy" | "2sell" | "3buy" | "3sell";
  price: number;
  approximate: boolean;
}

export type ChanDecisionBias = "long" | "neutral" | "risk";

export type ChanDecisionSetup = "bsp_buy" | "zs_breakout" | "none";

export type ChanDecisionState = "watching" | "pending" | "confirmed" | "invalid";

export type ChanDecisionIneligibleReason =
  | "no_actionable_structure"
  | "signal_bar_missing"
  | "signal_invalidated"
  | "stale_signal"
  | "risk_structure";

export interface ChanDecision {
  bias: ChanDecisionBias;
  setup: ChanDecisionSetup;
  state: ChanDecisionState;
  bsp_type: ChanBsp["type"] | null;
  signal_dt: string | null;
  bars_since_signal: number | null;
  confirm_price: number | null;
  invalidation_price: number | null;
  trigger_price: number | null;
  trigger_low: number | null;
  trigger_high: number | null;
  candidate_eligible: boolean;
  ineligible_reason: ChanDecisionIneligibleReason | null;
  basis: string[];
}

export interface ChanSummary {
  bars_end_dt?: string;
  /** 近期有机械跳空（除权除息），zs_break 已只用跳空后中枢 */
  ex_div_gap: boolean;
  last_bi_direction?: "up" | "down" | null;
  last_bi_days?: number;
  last_bi_confirmed?: boolean | null;
  last_confirmed_zs?: ChanZs | null;
  extending_zs?: ChanZs | null;
  zs_break?: "up" | "down" | "inside" | "none";
  recent_bsp?: ChanBsp | null;
  /** 降级原因（bars<30）：K 线照画，不报错页 */
  reason?: "insufficient_bars";
}

export interface ChanStructure {
  ts_code: string;
  name: string;
  freq: ChanFreq;
  bars: ChanBar[];
  bi_list: ChanBi[];
  zs_list: ChanZs[];
  bsp_list: ChanBsp[];
  summary: ChanSummary;
  decision: ChanDecision;
}

export async function getChanStructure(
  tsCode: string,
  freq: ChanFreq = "D",
  n = 250,
): Promise<ChanStructure> {
  return api.get<ChanStructure>(
    `/chan/${encodeURIComponent(tsCode)}?freq=${freq}&n=${n}`,
  );
}
