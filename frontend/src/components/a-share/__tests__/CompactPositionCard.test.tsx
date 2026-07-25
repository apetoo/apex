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

  it("plan.last_action + scale_plan -> 渲染最近建议(现在) + 未来触发计划", async () => {
    const posWithPlan = {
      ...pos,
      stop_loss: 32.0,
      plan: {
        scale_plan: [
          { level: 1, trigger_price: 36.5, action: "add", shares: 200, new_stop: 34.0 },
        ],
        doctrine: "single_v1",
        updated_at: "2026-07-24T18:53:06+08:00",
        last_action: "hold",
        last_new_stop: 32.5,
        last_stop_before: 32.0,
      },
    } as unknown as ActivePosition;
    withClient(<CompactPositionCard position={posWithPlan} />);
    await screen.findByText("100.00");
    // 持仓建议 header + 现在 行
    expect(screen.getByText("持仓建议")).toBeTruthy();
    expect(screen.getByText("现在")).toBeTruthy();
    expect(screen.getAllByText("持有").length).toBeGreaterThanOrEqual(1);
    // 止损方向（32.0->32.5 = ↑收紧，红）
    expect(screen.getByText("↑收紧")).toBeTruthy();
    // 未来触发计划 ladder（非现役指令）
    expect(screen.getByText(/未来触发计划/)).toBeTruthy();
    expect(screen.getByText("@36.50")).toBeTruthy();  // 触发价在前
    expect(screen.getByText("加")).toBeTruthy();  // 动作在后
  });

  it("止损下调 -> ↓放宽，一眼看出方向（防 AI 上移/下移说反）", async () => {
    const posLoosen = {
      ...pos,
      stop_loss: 21.5,
      plan: {
        scale_plan: [],
        doctrine: "single_v1",
        updated_at: "2026-07-25T17:21:00+08:00",
        last_action: "hold",
        last_new_stop: 21.5,
        last_stop_before: 22.0,
      },
    } as unknown as ActivePosition;
    withClient(<CompactPositionCard position={posLoosen} />);
    await screen.findByText("100.00");
    // 止损方向（22.0->21.5 = ↓放宽，绿）
    expect(screen.getByText("↓放宽")).toBeTruthy();
  });
});
