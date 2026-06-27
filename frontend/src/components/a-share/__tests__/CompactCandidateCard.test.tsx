import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { CompactCandidateCard } from "../CompactCandidateCard";
import type { Candidate } from "@/api/watchlist";

// mock 行情: 当前价可逐用例覆盖
const mockPrice: Record<string, number> = {};
vi.mock("@/api/market", () => ({
  getPrices: vi.fn(async (codes: string[]) =>
    Object.fromEntries(codes.map((c) => [c, mockPrice[c] ?? 22.15]))),
}));

function withClient(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const base: Candidate = {
  ts_code: "601012.SH",
  name: "隆基绿能",
  trigger_price: 22.5,
  trigger_direction: "below",
  expires_at: "2026-12-31",
  note: "",
  stop_advice: 21.0,
  target_advice: 25.0,
};

describe("CompactCandidateCard", () => {
  it("未触发 → 距触发行灰字(text-text-secondary)", async () => {
    mockPrice["601012.SH"] = 23.0; // 高于触发价 22.5, below 未触发, |pct|>2
    withClient(<CompactCandidateCard candidate={base} />);
    expect(screen.getByText("隆基绿能")).toBeTruthy();
    const distEl = await screen.findByText(/距触发/);
    expect(distEl.className).toContain("text-text-secondary");
    expect(screen.queryByText("已触发")).toBeNull();
  });

  it("接近触发(|pct|<2) → 距触发行黄字(text-amber-600)", async () => {
    mockPrice["601012.SH"] = 22.2; // (22.2-22.5)/22.5 = -1.33% → 接近
    withClient(<CompactCandidateCard candidate={base} />);
    const distEl = await screen.findByText(/距触发/);
    expect(distEl.className).toContain("text-amber-600");
  });

  it("已触发 → 卡片浅红底 + 已触发红字, 不显示距触发", async () => {
    mockPrice["601012.SH"] = 22.0; // <= 22.5 → below 触发
    const { container } = render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <CompactCandidateCard candidate={base} />
      </QueryClientProvider>,
    );
    // 卡片根 div 带 bg-up/5
    await screen.findByText("已触发");
    const cardRoot = container.firstChild as HTMLElement;
    expect(cardRoot.className).toContain("bg-up/5");
    expect(screen.queryByText(/距触发/)).toBeNull();
  });

  it("渲染触发价/方向 + 建议止损/目标", async () => {
    mockPrice["601012.SH"] = 23.0;
    withClient(<CompactCandidateCard candidate={base} />);
    await screen.findByText(/距触发/);
    expect(screen.getByText(/22\.50/)).toBeTruthy(); // 触发价
    expect(screen.getByText(/下方/)).toBeTruthy();
    expect(screen.getByText("21.00")).toBeTruthy(); // 建议止损
    expect(screen.getByText("25.00")).toBeTruthy(); // 目标
  });
});
