import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { SectorSentimentPage } from "../SectorSentimentPage";

const { getSectorSentimentCreators, getSectorSentimentDetail, moderateSectorSentimentCreator } = vi.hoisted(() => ({
  getSectorSentimentCreators: vi.fn(),
  getSectorSentimentDetail: vi.fn(),
  moderateSectorSentimentCreator: vi.fn(),
}));

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
  getSectorSentimentDetail,
  getSectorSentimentCreators,
  moderateSectorSentimentCreator,
}));

getSectorSentimentDetail.mockResolvedValue({
  history: [{ trade_date: "2026-08-10", short_risk: 88, swing_risk: 71 }],
  sector: { evidence: [{ platform: "bili", text: "机器人必须起飞", stance: 1 }] },
  retrieval_funnel: {
    raw_recalled: 10,
    financial_relevant: 4,
    filtered: 6,
    search_sources: 3,
    creator_sources: 1,
  },
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SectorSentimentPage />
    </QueryClientProvider>,
  );
}

function creator(overrides = {}) {
  return {
    platform: "bili",
    creator_id: "up-1",
    display_name: "财经小王",
    status: "candidate" as const,
    financial_ratio: 0.8,
    valid_content_count: 12,
    sector_ids: ["concept:robot"],
    last_discovered_at: "2026-08-10T09:00:00Z",
    evidence: [{ text: "机器人产业链景气改善" }],
    last_collection_error: null,
    ...overrides,
  };
}

test("approves a candidate and refreshes the creator pool", async () => {
  getSectorSentimentCreators.mockResolvedValue({ creators: [creator()] });
  moderateSectorSentimentCreator.mockResolvedValue(undefined);
  renderPage();

  fireEvent.click(screen.getByRole("button", { name: "作者池" }));
  expect(await screen.findByText("财经小王")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "批准财经小王" }));
  await waitFor(() => expect(moderateSectorSentimentCreator).toHaveBeenCalledWith("bili", "up-1", "approve"));
  await waitFor(() => expect(getSectorSentimentCreators).toHaveBeenCalledTimes(2));
});

test("shows retrieval funnel in sector detail", async () => {
  renderPage();

  fireEvent.click(await screen.findByRole("button", { name: "查看机器人证据" }));
  expect(await screen.findByText("原始召回 10")).toBeInTheDocument();
  expect(screen.getByText("金融相关 4")).toBeInTheDocument();
  expect(screen.getByText("已过滤 6")).toBeInTheDocument();
  expect(screen.getByText("搜索来源 3")).toBeInTheDocument();
  expect(screen.getByText("作者来源 1")).toBeInTheDocument();
});

test("shows missing retrieval funnel telemetry instead of zero values", async () => {
  getSectorSentimentDetail.mockResolvedValueOnce({
    history: [{ trade_date: "2026-08-10", short_risk: 88, swing_risk: 71 }],
    sector: { evidence: [{ platform: "bili", text: "机器人必须起飞", stance: 1 }] },
    retrieval_funnel: null,
  });
  renderPage();

  fireEvent.click(await screen.findByRole("button", { name: "查看机器人证据" }));
  expect(await screen.findByText("暂无检索漏斗数据")).toBeInTheDocument();
  expect(screen.queryByText("原始召回 0")).not.toBeInTheDocument();
});

test("shows an empty state for a creator status without rows", async () => {
  getSectorSentimentCreators.mockResolvedValue({ creators: [] });
  renderPage();

  fireEvent.click(screen.getByRole("button", { name: "作者池" }));
  expect(await screen.findByText("暂无候选作者")).toBeInTheDocument();
});

test("shows a retryable error instead of the empty state when loading creators fails", async () => {
  getSectorSentimentCreators
    .mockRejectedValueOnce(new Error("作者池加载失败"))
    .mockResolvedValueOnce({ creators: [creator()] });
  renderPage();

  fireEvent.click(screen.getByRole("button", { name: "作者池" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("作者池加载失败");
  expect(screen.queryByText("暂无候选作者")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "重试加载作者池" }));
  expect(await screen.findByText("财经小王")).toBeInTheDocument();
});

test("restores a rejected creator to candidate status", async () => {
  getSectorSentimentCreators.mockResolvedValue({ creators: [creator({ status: "rejected", display_name: "财经小李" })] });
  moderateSectorSentimentCreator.mockResolvedValue(undefined);
  renderPage();

  fireEvent.click(screen.getByRole("button", { name: "作者池" }));
  fireEvent.click(screen.getByRole("button", { name: "已拒绝" }));
  expect(await screen.findByText("财经小李")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "恢复财经小李为候选" }));
  await waitFor(() => expect(moderateSectorSentimentCreator).toHaveBeenCalledWith("bili", "up-1", "restore"));
});

test("keeps a creator row and shows an alert when moderation fails", async () => {
  getSectorSentimentCreators.mockResolvedValue({ creators: [creator()] });
  moderateSectorSentimentCreator.mockRejectedValue(new Error("操作失败"));
  renderPage();

  fireEvent.click(screen.getByRole("button", { name: "作者池" }));
  expect(await screen.findByText("财经小王")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "批准财经小王" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("操作失败");
  expect(screen.getByText("财经小王")).toBeInTheDocument();
});

test("renders collection failure for an invalid creator without using its ID as label", async () => {
  getSectorSentimentCreators.mockResolvedValue({
    creators: [creator({ creator_id: "invalid-private-id", display_name: null, last_collection_error: "作者已失效，无法采集" })],
  });
  renderPage();

  fireEvent.click(screen.getByRole("button", { name: "作者池" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("作者已失效，无法采集");
  expect(screen.getByText("未公开显示名")).toBeInTheDocument();
  expect(screen.queryByText("invalid-private-id")).not.toBeInTheDocument();
});

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
