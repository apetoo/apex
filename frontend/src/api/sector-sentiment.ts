import { api } from "./client";

export type AlertState = "normal" | "observe" | "warning" | "resolved" | "insufficient_data";

export interface SectorSentimentScore {
  trade_date: string;
  sector_id: string;
  sector_name: string;
  taxonomy: "industry" | "concept";
  platforms: string[];
  independent_authors: number;
  mapping_confidence: number;
  sentiment_extreme: number;
  attention_acceleration: number;
  consensus_crowding: number;
  market_divergence: number;
  short_risk: number;
  swing_risk: number;
  state: AlertState;
  platform_contributions: Record<string, { records: number; net_sentiment: number; fomo: number; panic: number }>;
  evidence: Array<{ platform?: string; text?: string; stance?: number }>;
}

export interface SectorSentimentOverview {
  as_of: string | null;
  coverage: number;
  data_quality: "ok" | "degraded" | "no_data";
  model_version: string;
  rule_version: string;
  shadow_mode: boolean;
  sectors: SectorSentimentScore[];
  changes: Array<{ event_id: string; sector_name: string; event_type: string; trade_date: string }>;
}

export interface SectorSentimentValidation {
  status: "accumulating" | "ready";
  trading_days: number;
  target_days: number;
  go_no_go: "PENDING" | "MIXED" | "GO" | "NO-GO";
  short: { n: number; precision: number | null };
  swing: { n: number; precision: number | null };
}

export interface SectorSentimentDetail {
  history: SectorSentimentScore[];
  sector: SectorSentimentScore;
}

export function getSectorSentimentOverview(date?: string) {
  return api.get<SectorSentimentOverview>(
    date ? `/sector-sentiment/overview?date=${date}` : "/sector-sentiment/overview",
  );
}

export function getSectorSentimentValidation() {
  return api.get<SectorSentimentValidation>("/sector-sentiment/validation");
}

export function getSectorSentimentDetail(sectorId: string, date?: string) {
  return api.get<SectorSentimentDetail>(
    `/sector-sentiment/${encodeURIComponent(sectorId)}${date ? `?date=${date}` : ""}`,
  );
}
