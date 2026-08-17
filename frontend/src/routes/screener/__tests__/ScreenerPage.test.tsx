import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/a-share/FactorIcCard", () => ({
  FactorIcCard: () => null,
}));

vi.mock("@/hooks/useChatContext", () => ({
  useChatContext: () => ({ setContext: vi.fn() }),
}));

const connect = vi.fn();

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({
    events: [],
    status: "idle",
    error: null,
    result: null,
    connect,
    abort: vi.fn(),
    reset: vi.fn(),
  }),
}));

vi.mock("@/api/screener", async () => {
  const actual = await vi.importActual<typeof import("@/api/screener")>("@/api/screener");
  return {
    ...actual,
    getStrategies: vi.fn(),
    getDefaultWeights: vi.fn(),
    getAvailableDates: vi.fn(),
  };
});

import {
  getAvailableDates,
  getDefaultWeights,
  getStrategies,
} from "@/api/screener";
import { ScreenerPage } from "../ScreenerPage";

const strategyNames = [
  "first_board_leader",
  "institutional_flow",
  "industry_rotation",
  "leader_with_volume",
  "pullback_to_ma",
  "bullish_alignment",
  "volume_breakout",
  "stealth_accumulation",
];

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ScreenerPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  connect.mockReset();
  vi.mocked(getStrategies).mockResolvedValue(
    strategyNames.map((name) => ({ name, description: `${name} description` })),
  );
  vi.mocked(getDefaultWeights).mockResolvedValue(
    Object.fromEntries(strategyNames.map((name) => [name, 0.125])),
  );
  vi.mocked(getAvailableDates).mockResolvedValue([]);
});

describe("ScreenerPage strategy mode", () => {
  it("starts new users in automatic mode without manual sliders", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("radio", { name: "自动适配（推荐）" })).toBeChecked();
    });
    expect(
      screen.getByText("系统将根据市场状态、历史表现和今日候选数自动分配权重；失败时回退等权。"),
    ).toBeInTheDocument();
    expect(screen.queryAllByRole("slider")).toHaveLength(0);
  });

  it("applies an editable manual preset and returns to automatic requests", async () => {
    renderPage();
    const auto = await screen.findByRole("radio", { name: "自动适配（推荐）" });
    await waitFor(() => expect(screen.getByRole("button", { name: "跑粗筛" })).toBeEnabled());

    fireEvent.click(screen.getByRole("radio", { name: "稳健波段" }));

    const sliders = await screen.findAllByRole("slider");
    expect(sliders).toHaveLength(8);
    const stealth = screen.getByRole("slider", { name: "stealth_accumulation" });
    expect(stealth).toHaveValue("0.3");

    fireEvent.change(stealth, { target: { value: "0.35" } });
    await waitFor(() => {
      expect(
        JSON.parse(window.localStorage.getItem("apex.screener.preferences") ?? "null"),
      ).toMatchObject({
        mode: "steady",
        weights: { stealth_accumulation: 0.35 },
      });
    });

    fireEvent.click(screen.getByRole("button", { name: "跑粗筛" }));
    expect(connect).toHaveBeenLastCalledWith(
      expect.objectContaining({
        body: expect.objectContaining({
          strategy_weights: expect.objectContaining({ stealth_accumulation: 0.35 }),
        }),
      }),
    );

    fireEvent.click(auto);
    expect(screen.queryAllByRole("slider")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "跑粗筛" }));
    expect(connect).toHaveBeenLastCalledWith(
      expect.objectContaining({
        body: { skip_ai: false, skip_selector: false },
      }),
    );
  });
});
