import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { SectorSentimentPage } from "../SectorSentimentPage";

vi.mock("@/api/sector-sentiment", () => ({
  getSectorSentimentOverview: vi.fn().mockResolvedValue({
    as_of: "2026-08-10",
    coverage: 1,
    data_quality: "ok",
    model_version: "model-v1",
    rule_version: "rule-v1",
    shadow_mode: true,
    sectors: [
      {
        trade_date: "2026-08-10",
        sector_id: "concept:robot",
        sector_name: "机器人",
        taxonomy: "concept",
        platforms: ["bili", "eastmoney"],
        independent_authors: 20,
        mapping_confidence: 0.9,
        sentiment_extreme: 0.95,
        attention_acceleration: 0.92,
        consensus_crowding: 0.91,
        market_divergence: 0.75,
        short_risk: 88,
        swing_risk: 71,
        state: "warning",
        platform_contributions: {},
        evidence: [],
      },
    ],
    changes: [],
  }),
  getSectorSentimentValidation: vi.fn().mockResolvedValue({
    status: "accumulating",
    trading_days: 12,
    target_days: 60,
    go_no_go: "PENDING",
    short: { n: 5, precision: 0.6 },
    swing: { n: 2, precision: 0.5 },
  }),
  getSectorSentimentDetail: vi.fn().mockResolvedValue({
    history: [{ trade_date: "2026-08-10", short_risk: 88, swing_risk: 71 }],
    sector: { evidence: [{ platform: "bili", text: "机器人必须起飞", stance: 1 }] },
  }),
}));

test("renders warning sector with two horizons and shadow disclaimer", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SectorSentimentPage />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("机器人")).toBeInTheDocument();
  expect(screen.getByText("警戒")).toBeInTheDocument();
  expect(screen.getByText("短线 88")).toBeInTheDocument();
  expect(screen.getByText("波段 71")).toBeInTheDocument();
  expect(screen.getByText(/研究观察，不触发交易/)).toBeInTheDocument();
  expect(screen.getByText("12 / 60 个交易日")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看机器人证据" }));
  expect(await screen.findByText("机器人必须起飞")).toBeInTheDocument();
  expect(screen.getByText("60日风险轨迹")).toBeInTheDocument();
});
