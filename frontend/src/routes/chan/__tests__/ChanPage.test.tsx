import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";

// 图表组件依赖 lightweight-charts canvas，jsdom 下不可用 -> 整体 mock
vi.mock("@/components/a-share/CandlestickChart", () => ({
  CandlestickChart: () => <div data-testid="chart" />,
}));

// api 层 mock：getChanStructure 由各用例 mockResolvedValue/mockRejectedValue 控制
vi.mock("@/api/chan", async () => {
  const actual = await vi.importActual<typeof import("@/api/chan")>("@/api/chan");
  return { ...actual, getChanStructure: vi.fn() };
});

import { ChanPage } from "../ChanPage";
import { getChanStructure, type ChanStructure } from "@/api/chan";
import { ApiError } from "@/api/client";

/**
 * /chan 页前端测试（T8）
 *
 * 覆盖：渲染态 + 4 错误/降级态（501/502/insufficient/ex_div）+
 * 标记映射 + 免责声明存在性 + BJ 分钟切换禁用。
 */

const MOCK_BAR = { dt: "2026-08-07", open: 87, high: 89, low: 86, close: 88, vol: 100 };

function fullDecision() {
  return {
    bias: "long" as const,
    setup: "bsp_buy" as const,
    state: "pending" as const,
    bsp_type: "2buy" as const,
    signal_dt: "2026-08-05",
    bars_since_signal: 2,
    confirm_price: 89,
    invalidation_price: 85,
    trigger_price: 88,
    trigger_low: 87,
    trigger_high: 89,
    candidate_eligible: true,
    ineligible_reason: null,
    basis: ["二买信号待确认"],
  };
}

function fullStructure(over: Partial<ChanStructure> = {}): ChanStructure {
  return {
    ts_code: "603019.SH",
    freq: "D",
    bars: [MOCK_BAR],
    bi_list: [{ sdt: "2026-08-01", edt: "2026-08-07", direction: "up", high: 89, low: 85, confirmed: true }],
    zs_list: [{ sdt: "2026-07-01", edt: "2026-07-20", zg: 90, zd: 80, zz: 85, state: "confirmed" }],
    bsp_list: [{ dt: "2026-08-05", type: "1buy", price: 86, approximate: true }],
    summary: {
      bars_end_dt: "2026-08-07",
      ex_div_gap: false,
      last_bi_direction: "up",
      last_bi_days: 5,
      last_bi_confirmed: true,
      last_confirmed_zs: { sdt: "2026-07-01", edt: "2026-07-20", zg: 90, zd: 80, zz: 85, state: "confirmed" },
      extending_zs: null,
      zs_break: "up",
      recent_bsp: { dt: "2026-08-05", type: "1buy", price: 86, approximate: true },
    },
    decision: fullDecision(),
    ...over,
  };
}

function withClient(ui: ReactNode, initialPath = "/chan") {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => vi.restoreAllMocks());

describe("ChanPage 渲染态", () => {
  it("完整结构 -> 图表 + 摘要 + 指标", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure());
    withClient(<ChanPage />);
    await waitFor(() => expect(screen.getByTestId("chart")).toBeTruthy());
    expect(screen.getByText("603019.SH")).toBeTruthy();
    expect(screen.getByText("突破向上")).toBeTruthy();
    expect(screen.getByText(/80\.00 ~ 90\.00/)).toBeTruthy();
  });

  it("口径披露包含本级别近似、重画和 250 根窗口滑动", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure());
    withClient(<ChanPage />);
    await waitFor(() => expect(screen.getByTestId("chart")).toBeTruthy());
    expect(screen.getByText(/本级别近似/)).toBeTruthy();
    expect(screen.getByText(/重画/)).toBeTruthy();
    expect(screen.getByText(/250 根窗口/)).toBeTruthy();
    expect(screen.getByText(/窗口滑动后/)).toBeTruthy();
    expect(screen.getByText(/只使用已完成 K 线/)).toBeTruthy();
  });

  it("摘要提供加入候选和 AI 分析入口", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure());
    withClient(<ChanPage />);
    await waitFor(() => expect(screen.getByTestId("chart")).toBeTruthy());

    expect(screen.getByRole("link", { name: "加入候选" }).getAttribute("href")).toBe("/watchlist");
    const analyze = screen.getByRole("link", { name: "发起 AI 分析" });
    expect(analyze.getAttribute("href")).toBe("/analyze");
    fireEvent.click(analyze);
    expect(JSON.parse(sessionStorage.getItem("apex.analyze.state") ?? "{}").tsCode).toBe("603019.SH");
  });
});

describe("ChanPage 标记映射", () => {
  it("recent_bsp 类型映射到中文标签", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(
      fullStructure({
        bsp_list: [{ dt: "2026-08-05", type: "3buy", price: 86, approximate: true }],
        summary: {
          ...fullStructure().summary,
          recent_bsp: { dt: "2026-08-05", type: "3buy", price: 86, approximate: true },
        },
      }),
    );
    withClient(<ChanPage />);
    await waitFor(() => expect(screen.getByText(/三买/)).toBeTruthy());
  });
});

describe("ChanPage 4 错误/降级态", () => {
  it("501 czsc 不可用 -> 缠论引擎不可用", async () => {
    vi.mocked(getChanStructure).mockRejectedValue(new ApiError(501, "czsc 未安装"));
    withClient(<ChanPage />);
    await waitFor(() => expect(screen.getByText(/缠论引擎不可用/)).toBeTruthy());
    expect(screen.getByText(/czsc==0.10.12/)).toBeTruthy();
  });

  it("502 数据失败 -> 数据获取失败", async () => {
    vi.mocked(getChanStructure).mockRejectedValue(new ApiError(502, "tushare empty"));
    withClient(<ChanPage />);
    await waitFor(() => expect(screen.getByText(/数据获取失败/)).toBeTruthy());
  });

  it("insufficient_bars -> 历史数据不足降级", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(
      fullStructure({ bi_list: [], zs_list: [], bsp_list: [],
        summary: { reason: "insufficient_bars", ex_div_gap: false, bars_end_dt: "2026-08-07" } }),
    );
    withClient(<ChanPage />);
    await waitFor(() => expect(screen.getByText(/历史数据不足/)).toBeTruthy());
  });

  it("ex_div_gap -> 除权警示", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(
      fullStructure({ summary: { ...fullStructure().summary, ex_div_gap: true } }),
    );
    withClient(<ChanPage />);
    await waitFor(() => expect(screen.getByText(/除权除息跳空/)).toBeTruthy());
  });
});

describe("ChanPage BJ 分钟降级", () => {
  it("BJ 票 30/60 分切换禁用", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(
      fullStructure({ ts_code: "920810.BJ", freq: "D" }),
    );
    withClient(<ChanPage />);
    // 切到 BJ 代码
    const input = screen.getByPlaceholderText(/603019.SH/) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "920810.BJ" } });
    fireEvent.click(screen.getByRole("button", { name: "查看" }));
    await waitFor(() => {
      const btn30 = screen.getByText("30分").closest("button") as HTMLButtonElement;
      expect(btn30.disabled).toBe(true);
    });
  });
});

describe("ChanPage URL 参数预填", () => {
  it("?ts_code= 预填并自动加载", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure({ ts_code: "510300.SH" }));
    withClient(<ChanPage />, "/chan?ts_code=510300.SH");
    await waitFor(() => expect(screen.getByText("510300.SH")).toBeTruthy());
    expect(getChanStructure).toHaveBeenCalledWith("510300.SH", "D");
  });
});
