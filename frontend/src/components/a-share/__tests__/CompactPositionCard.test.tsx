import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { CompactPositionCard } from "../CompactPositionCard";
import type { ActivePosition } from "@/api/watchlist";

// mock 行情: 600519.SH 当前 100.0, 昨收 98.0
vi.mock("@/api/market", () => ({
  getPrices: vi.fn(async (codes: string[]) =>
    Object.fromEntries(codes.map((c) => [c, 100.0]))),
  getDailyPrices: vi.fn(async (codes: string[]) =>
    Object.fromEntries(codes.map((c) => [c, 98.0]))),
}));

function withClient(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const pos = {
  ts_code: "600519.SH",
  name: "贵州茅台",
  entry_price: 90,
  avg_cost: 90,
  stop_loss: 85,
  target: 110,
  position_size_shares: 100,
  strategy: "价值",
} as unknown as ActivePosition;

describe("CompactPositionCard", () => {
  it("渲染名称/代码/策略, 当前价黑色(非红绿)", async () => {
    withClient(<CompactPositionCard position={pos} />);
    expect(screen.getByText("贵州茅台")).toBeTruthy();
    expect(screen.getByText("价值")).toBeTruthy();
    expect(screen.getByText("600519.SH")).toBeTruthy();
    const priceEl = await screen.findByText("100.00");
    expect(priceEl.className).toContain("text-text-primary");
    expect(priceEl.className).not.toContain("text-up");
    expect(priceEl.className).not.toContain("text-down");
  });

  it("只读(未传 onAdd/onReduce) → 不渲染加仓/减仓按钮", async () => {
    withClient(<CompactPositionCard position={pos} />);
    await screen.findByText("100.00");
    expect(screen.queryByText("加仓")).toBeNull();
    expect(screen.queryByText("减仓")).toBeNull();
  });

  it("传 onAdd/onReduce → 渲染按钮且可点", async () => {
    const onAdd = vi.fn();
    const onReduce = vi.fn();
    withClient(
      <CompactPositionCard position={pos} onAdd={onAdd} onReduce={onReduce} />,
    );
    const addBtn = await screen.findByText("加仓");
    expect(screen.getByText("减仓")).toBeTruthy();
    addBtn.click();
    expect(onAdd).toHaveBeenCalledWith(pos);
  });

  it("成本/止损/目标参数行渲染", async () => {
    withClient(<CompactPositionCard position={pos} />);
    await screen.findByText("100.00");
    // 100 股 @ 90.00
    expect(screen.getByText(/100\s*股/)).toBeTruthy();
    expect(screen.getByText(/90\.00/)).toBeTruthy();
    // 止损 85.00 / 目标 110.00
    expect(screen.getByText("85.00")).toBeTruthy();
    expect(screen.getByText("110.00")).toBeTruthy();
  });
});
