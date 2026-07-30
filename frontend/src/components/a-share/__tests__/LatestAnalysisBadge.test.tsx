import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { LatestAnalysisBadge } from "../LatestAnalysisBadge";
import { getLatestJournal } from "@/api/analyze";

vi.mock("@/api/analyze", () => ({
  getLatestJournal: vi.fn(async () => null),
}));

const mocked = vi.mocked(getLatestJournal);

function withClient(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const verdictEntry = {
  ts_code: "600519.SH",
  analyzed_at: new Date(Date.now() - 86400000).toISOString(), // 1 天前, 不陈旧
  source: "verdict",
  verdict: "看多",
  confidence: 68,
  calibrated_confidence: 62,
  evidence: ["业绩预增40%,北向连续3日增持", "回踩20日线企稳,量能配合", "第三条不该显示"],
  price_advice: { entry: null, entry_low: 12.3, entry_high: 12.6, stop_loss: 11.8, target: 14.5 },
};

const paEntry = {
  ts_code: "600519.SH",
  analyzed_at: new Date(Date.now() - 86400000).toISOString(),
  source: "position_action",
  position_action: {
    action: "trim",
    trim_shares: 300,
    new_stop: 12.8,
    rationale: "放量滞涨,龙头地位被挑战,先锁定部分利润。",
    scale_plan: [{ level: 1 }, { level: 2 }],
  },
};

describe("LatestAnalysisBadge", () => {
  it("无 journal -> 不渲染任何内容", async () => {
    mocked.mockResolvedValueOnce(null);
    const { container } = withClient(<LatestAnalysisBadge tsCode="600519.SH" />);
    // 等 query settle
    await new Promise((r) => setTimeout(r, 0));
    expect(container.innerHTML).toBe("");
  });

  it("verdict entry -> 徽标: 看多 + 校准62 + 日期; 浮窗: 三价 + 前2条证据", async () => {
    mocked.mockResolvedValueOnce(verdictEntry as never);
    withClient(<LatestAnalysisBadge tsCode="600519.SH" />);
    // 看多 在徽标 + popover + drawer(隐藏) 三处都渲染; 取首条即徽标
    const matches = await screen.findAllByText("看多");
    expect(matches[0]).toBeTruthy();
    // 校准 62 出现 3 处(徽标 + popover + drawer), 至少 1 次即视为通过
    expect(screen.getAllByText(/校准\s*62/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/^\d{2}-\d{2}$/)).toBeTruthy(); // 日期 MM-DD(仅徽标)
    // 浮窗内容在 DOM 中(group-hover 只是 CSS 隐藏, jsdom 可直接断言)
    // 价格类文本 popover + drawer 都有, 用 getAllByText 断言至少 1 次
    expect(screen.getAllByText(/入场 12.30–12.60/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/止损 11.80/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/目标 14.50/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/业绩预增40%/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/回踩20日线/).length).toBeGreaterThanOrEqual(1);
    // 只取前 2 条: 第三条只出现在 drawer(popover 限 2), 全局恰好 1 次
    expect(screen.getAllByText(/第三条不该显示/).length).toBe(1);
    expect(screen.getAllByText(/点击查看完整分析/).length).toBeGreaterThanOrEqual(1);
  });

  it("position_action entry -> 徽标: 减仓 + 止损; 浮窗: rationale + 触发计划条数", async () => {
    mocked.mockResolvedValueOnce(paEntry as never);
    withClient(<LatestAnalysisBadge tsCode="600519.SH" stopBefore={12.3} />);
    // 减仓 在徽标 + popover 两处都渲染; 取首条即徽标
    const matches = await screen.findAllByText("减仓");
    expect(matches[0]).toBeTruthy();
    expect(screen.getAllByText(/12\.80/).length).toBeGreaterThanOrEqual(1);
    // 浮窗 + drawer 都渲染这些字段, 用 getAllByText 至少 1 次
    expect(screen.getAllByText(/-300股/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/放量滞涨/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/未来触发计划 2 条/).length).toBeGreaterThanOrEqual(1);
    // stopBefore=12.3 < 12.8 -> 收紧(红涨: 收紧=up)
    expect(screen.getAllByText("↑收紧").length).toBeGreaterThanOrEqual(1);
  });

  it("陈旧(>5 天) -> 显示 N 天前", async () => {
    mocked.mockResolvedValueOnce({
      ...verdictEntry,
      analyzed_at: new Date(Date.now() - 12 * 86400000).toISOString(),
    } as never);
    withClient(<LatestAnalysisBadge tsCode="600519.SH" />);
    expect(await screen.findByText(/天前/)).toBeTruthy();
  });

  it("点击徽标 -> Drawer 打开渲染 VerdictDetailCard", async () => {
    mocked.mockResolvedValueOnce(verdictEntry as never);
    withClient(<LatestAnalysisBadge tsCode="600519.SH" />);
    const matches = await screen.findAllByText("看多");
    fireEvent.click(matches[0].closest("button")!);
    // Drawer 标题 + VerdictDetailCard 的"分析结果"
    expect(await screen.findByText("最近分析")).toBeTruthy();
    expect(screen.getByText("分析结果")).toBeTruthy();
    expect(screen.getByText("证据链")).toBeTruthy();
  });
});
