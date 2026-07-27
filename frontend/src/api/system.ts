/**
 * System API - 「我的交易系统」(ADR-0001 / ADR-0002)
 *
 * GET  /api/system          - 读 system.json（无则 null）
 * POST /api/system/recompute - 重算并落盘
 *
 * 字段形状以 apex/system.py:compute() 的返回为准。
 */

import { api } from "./client";

export type Confidence =
  | "ok"
  | "low_sample_descriptive_only"
  | "accumulating"
  | "no_data";

export interface RateMetric {
  n: number;
  count: number;
  rate: number | null;
  confidence: Confidence;
}

export interface FlagMetric {
  n: number;
  confidence: Confidence;
}

export interface SystemView {
  computed_at: string;
  sample: {
    closed_n: number;
    trades_n: number;
    journal_n: number;
    eligible_n: number;
  };
  thresholds: { attribution_n_min: number; expectancy_n_min: number };
  trading_dna: {
    verdict_distribution: Record<string, number>;
    strategy_distribution: Record<string, number>;
    setup_distribution: Record<string, number>;
    regime_distribution: Record<string, number>;
    sector_concentration: {
      top: { name: string; count: number; share: number } | null;
      distribution: Record<string, number>;
      n: number;
      confidence: Confidence;
    };
    avg_hold_days: { value: number | null; n: number; confidence: Confidence };
  };
  behavior: {
    chase: RateMetric;
    average_down: RateMetric;
    stop_discipline: {
      n: number;
      no_stop_n: number;
      honored_n: number;
      override_breach_n: number;
      not_triggered_n: number;
      confidence: Confidence;
    };
    take_profit_discipline: {
      n: number;
      no_target_n: number;
      honored_n: number;
      missed_n: number;
      not_hit_n: number;
      confidence: Confidence;
    };
    hold_period_distribution: {
      distribution: Record<string, number>;
      n: number;
      confidence: Confidence;
    };
    proxy_emotional: {
      revenge: RateMetric;
      fomo: RateMetric;
      overtrading: RateMetric;
    };
    position_action_adherence: {
      follow_n: number;
      deviate_n: number;
      partial_n: number;
      na_n: number;
      null_n: number;
      stale_n: number;
      actionable_n: number;
      n: number;
      follow_rate: number | null;
      confidence: Confidence;
    };
  };
  ai_adherence: {
    per_trade: Array<{
      ts_code: string;
      name: string;
      entry_band_ok: boolean | null;
      stop_set: boolean;
      stop_honored: boolean | null;
      has_ai_plan: boolean;
      score: number | null;
    }>;
    aggregate: {
      entry_band_rate: number | null;
      stop_set_rate: number | null;
      stop_honored_rate: number | null;
      n: number;
      confidence: Confidence;
    };
  };
  discipline_score: {
    per_trade: Array<{
      ts_code: string;
      name: string;
      score: number | null;
      has_user_rule: boolean;
    }>;
    rolling_avg: { value: number | null; n: number; confidence: Confidence };
  };
  tier2_status: Record<string, unknown> & { note?: string };
}

/** GET /api/system -> SystemView | null（无 system.json 时后端返回 null） */
export async function getSystem(): Promise<SystemView | null> {
  return api.get<SystemView | null>("/system");
}

/** POST /api/system/recompute -> 重算后的 SystemView */
export async function recomputeSystem(): Promise<SystemView> {
  return api.post<SystemView>("/system/recompute");
}
