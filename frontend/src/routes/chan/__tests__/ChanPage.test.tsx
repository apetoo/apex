import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
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

vi.mock("@/api/watchlist", async () => {
  const actual = await vi.importActual<typeof import("@/api/watchlist")>("@/api/watchlist");
  return { ...actual, getWatchlist: vi.fn() };
});

vi.mock("@/api/mutations", () => ({
  useAddCandidate: vi.fn(),
}));

import { ChanPage } from "../ChanPage";
import { getChanStructure, type ChanDecision, type ChanStructure } from "@/api/chan";
import { ApiError } from "@/api/client";
import { useAddCandidate } from "@/api/mutations";
import { getWatchlist } from "@/api/watchlist";

/**
 * /chan 页前端测试（T8）
 *
 * 覆盖：渲染态 + 4 错误/降级态（501/502/insufficient/ex_div）+
 * 标记映射 + 免责声明存在性 + BJ 分钟切换禁用。
 */

const MOCK_BAR = { dt: "2026-08-07", open: 87, high: 89, low: 86, close: 88, vol: 100 };

function fullDecision(overrides: Partial<ChanDecision> = {}): ChanDecision {
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
    ...overrides,
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

const addCandidateMutate = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  addCandidateMutate.mockReset();
  vi.mocked(getWatchlist).mockResolvedValue({
    active_positions: [],
    candidates: [],
    archived: [],
  });
  vi.mocked(useAddCandidate).mockReturnValue({
    mutate: addCandidateMutate,
    isPending: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof useAddCandidate>);
});

describe("ChanPage fixture", () => {
  it("allows a single decision field to be overridden", () => {
    expect(fullDecision({ state: "confirmed" }).state).toBe("confirmed");
  });
});

function inputValue(label: string) {
  return (screen.getByLabelText(label) as HTMLInputElement).value;
}

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

  it("决策卡提供加入候选操作并保留 AI 分析入口", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure());
    withClient(<ChanPage />);
    await waitFor(() => expect(screen.getByTestId("chart")).toBeTruthy());

    expect(screen.getByRole("button", { name: "加入候选" })).toBeEnabled();
    const analyze = screen.getByRole("link", { name: "发起 AI 分析" });
    expect(analyze.getAttribute("href")).toBe("/analyze");
    fireEvent.click(analyze);
    expect(JSON.parse(sessionStorage.getItem("apex.analyze.state") ?? "{}").tsCode).toBe("603019.SH");
  });
});

describe("ChanPage 缠论决策卡", () => {
  it("pending 显示确认条件、失效条件、全部依据与可用候选操作", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(
      fullStructure({
        decision: fullDecision({
          basis: ["二买信号待确认", "收盘站上确认价才成立"],
        }),
      }),
    );

    withClient(<ChanPage />);

    await waitFor(() => expect(screen.getByText("缠论决策卡")).toBeTruthy());
    expect(screen.getByText("等待确认")).toBeTruthy();
    expect(screen.getByText("偏多")).toBeTruthy();
    expect(screen.getByText("确认价").nextElementSibling?.textContent).toBe("89.00");
    expect(screen.getByText("失效价").nextElementSibling?.textContent).toBe("85.00");
    expect(screen.getByText("距今 2 根")).toBeTruthy();
    expect(screen.getByText("二买信号待确认")).toBeTruthy();
    expect(screen.getByText("收盘站上确认价才成立")).toBeTruthy();
    expect(screen.getByRole("button", { name: "加入候选" })).toBeEnabled();
    expect(screen.getByText("突破向上")).toBeTruthy();
    expect(screen.getByText(/80\.00 ~ 90\.00/)).toBeTruthy();
  });

  it("confirmed 显示已确认和反追高提醒", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(
      fullStructure({ decision: fullDecision({ state: "confirmed" }) }),
    );

    withClient(<ChanPage />);

    await waitFor(() => expect(screen.getAllByText("已确认").length).toBeGreaterThan(0));
    expect(screen.getByText(/不.*追高/)).toBeTruthy();
  });

  it("invalid 显示失效原因并禁止加入候选", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(
      fullStructure({
        decision: fullDecision({
          state: "invalid",
          candidate_eligible: false,
          ineligible_reason: "当前收盘已跌破结构失效价",
        }),
      }),
    );

    withClient(<ChanPage />);

    await waitFor(() => expect(screen.getByText("已失效")).toBeTruthy());
    expect(screen.getByText("当前收盘已跌破结构失效价")).toBeTruthy();
    expect(screen.getByRole("button", { name: "加入候选" })).toBeDisabled();
  });

  it.each([
    {
      name: "watching",
      decision: fullDecision({
        bias: "neutral",
        setup: "none",
        state: "watching",
        candidate_eligible: false,
        ineligible_reason: "暂无可执行结构",
      }),
      stateLabel: "观察中",
      biasLabel: "中性",
    },
    {
      name: "risk",
      decision: fullDecision({
        bias: "risk",
        state: "invalid",
        candidate_eligible: false,
        ineligible_reason: "出现卖点风险",
      }),
      stateLabel: "已失效",
      biasLabel: "风险",
    },
  ])("$name 显示方向和状态标签并禁止加入候选", async ({ decision, stateLabel, biasLabel }) => {
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure({ decision }));

    withClient(<ChanPage />);

    await waitFor(() => expect(screen.getByText(stateLabel)).toBeTruthy());
    expect(screen.getByText(biasLabel)).toBeTruthy();
    expect(screen.getByRole("button", { name: "加入候选" })).toBeDisabled();
  });

  it("nullable 决策价格显示占位符", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(
      fullStructure({
        decision: fullDecision({
          confirm_price: null,
          invalidation_price: null,
          bars_since_signal: null,
        }),
      }),
    );

    withClient(<ChanPage />);

    await waitFor(() => expect(screen.getByText("缠论决策卡")).toBeTruthy());
    expect(screen.getByText("确认价").nextElementSibling?.textContent).toBe("—");
    expect(screen.getByText("失效价").nextElementSibling?.textContent).toBe("—");
  });
});

describe("ChanPage 候选确认弹窗", () => {
  it("买点预填代码、周期、触发价、止损、setup 和备注并提交 chan payload", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(
      fullStructure({
        decision: fullDecision({
          trigger_price: 10.5,
          trigger_low: null,
          trigger_high: null,
          invalidation_price: 10,
        }),
      }),
    );
    withClient(<ChanPage />);

    fireEvent.click(await screen.findByRole("button", { name: "加入候选" }));

    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeTruthy();
    expect(screen.getAllByText("603019.SH").length).toBeGreaterThan(1);
    expect(within(dialog).getByText("日线")).toBeTruthy();
    expect(within(dialog).getByText("缠论二买")).toBeTruthy();
    expect(inputValue("触发价")).toBe("10.50");
    expect(inputValue("止损价")).toBe("10.00");
    expect(inputValue("备注")).toContain("2026-08-05");

    fireEvent.click(screen.getByRole("button", { name: "确认加入候选" }));

    expect(addCandidateMutate).toHaveBeenCalledTimes(1);
    const payload = addCandidateMutate.mock.calls[0][0];
    expect(payload).toEqual({
      ts_code: "603019.SH",
      name: "",
      trigger_price: 10.5,
      trigger_direction: "above",
      trigger_low: undefined,
      trigger_high: undefined,
      stop_advice: 10,
      note: expect.stringContaining("2026-08-05"),
      strategy: "chan",
      setup: "缠论二买",
    });
    expect(payload).not.toHaveProperty("target_advice");
  });

  it("中枢突破预填回踩区间并提交 below 兼容方向", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(
      fullStructure({
        decision: fullDecision({
          setup: "zs_breakout",
          state: "confirmed",
          bsp_type: null,
          trigger_price: 12,
          trigger_low: 12,
          trigger_high: 12.12,
          invalidation_price: 11.5,
        }),
      }),
    );
    withClient(<ChanPage />);

    fireEvent.click(await screen.findByRole("button", { name: "加入候选" }));

    expect(inputValue("触发价")).toBe("12.00");
    expect(inputValue("区间下沿")).toBe("12.00");
    expect(inputValue("区间上沿")).toBe("12.12");
    expect(screen.getByText("中枢突破回踩")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认加入候选" }));

    expect(addCandidateMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        trigger_price: 12,
        trigger_direction: "below",
        trigger_low: 12,
        trigger_high: 12.12,
        strategy: "chan",
        setup: "中枢突破回踩",
      }),
      expect.any(Object),
    );
  });

  it("提交采用人工修改后的价格和备注", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure());
    withClient(<ChanPage />);
    fireEvent.click(await screen.findByRole("button", { name: "加入候选" }));

    fireEvent.change(screen.getByLabelText("触发价"), { target: { value: "91.25" } });
    fireEvent.change(screen.getByLabelText("区间下沿"), { target: { value: "90.00" } });
    fireEvent.change(screen.getByLabelText("区间上沿"), { target: { value: "91.00" } });
    fireEvent.change(screen.getByLabelText("止损价"), { target: { value: "84.50" } });
    fireEvent.change(screen.getByLabelText("备注"), { target: { value: "人工复核后提交" } });
    fireEvent.click(screen.getByRole("button", { name: "确认加入候选" }));

    expect(addCandidateMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        trigger_price: 91.25,
        trigger_low: 90,
        trigger_high: 91,
        stop_advice: 84.5,
        note: "人工复核后提交",
      }),
      expect.any(Object),
    );
  });

  it("mutation 失败时保留弹窗、输入和错误信息", async () => {
    addCandidateMutate.mockImplementation(
      (_payload: unknown, options?: { onError?: (error: Error) => void }) => {
        options?.onError?.(new Error("保存失败"));
      },
    );
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure());
    withClient(<ChanPage />);
    fireEvent.click(await screen.findByRole("button", { name: "加入候选" }));
    fireEvent.change(screen.getByLabelText("备注"), { target: { value: "保留我的修改" } });

    fireEvent.click(screen.getByRole("button", { name: "确认加入候选" }));

    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(inputValue("备注")).toBe("保留我的修改");
    expect(screen.getByText(/保存失败/)).toBeTruthy();
  });

  it("mutation 成功时关闭弹窗并显示本地成功反馈", async () => {
    addCandidateMutate.mockImplementation(
      (_payload: unknown, options?: { onSuccess?: () => void }) => {
        options?.onSuccess?.();
      },
    );
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure());
    withClient(<ChanPage />);
    fireEvent.click(await screen.findByRole("button", { name: "加入候选" }));

    fireEvent.click(screen.getByRole("button", { name: "确认加入候选" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByText("已加入候选")).toBeTruthy();
  });

  it("同代码已有候选时在提交前提示会覆盖原参数", async () => {
    vi.mocked(getWatchlist).mockResolvedValue({
      active_positions: [],
      candidates: [{
        ts_code: "603019.SH",
        name: "中科曙光",
        trigger_price: 88,
        trigger_direction: "above",
        expires_at: "2026-08-20",
        note: "原候选",
        stop_advice: 85,
        target_advice: 95,
      }],
      archived: [],
    });
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure());
    withClient(<ChanPage />);

    fireEvent.click(await screen.findByRole("button", { name: "加入候选" }));

    expect(await screen.findByText("将覆盖原候选参数")).toBeTruthy();
  });

  it("候选列表读取失败时说明无法确认重复但仍允许提交", async () => {
    vi.mocked(getWatchlist).mockRejectedValue(new Error("读取失败"));
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure());
    withClient(<ChanPage />);

    fireEvent.click(await screen.findByRole("button", { name: "加入候选" }));

    expect(await screen.findByText(/无法确认是否已有候选/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认加入候选" }));
    expect(addCandidateMutate).toHaveBeenCalledTimes(1);
  });

  it("非正数触发价显示行内错误且不提交", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure());
    withClient(<ChanPage />);
    fireEvent.click(await screen.findByRole("button", { name: "加入候选" }));
    fireEvent.change(screen.getByLabelText("触发价"), { target: { value: "0" } });

    fireEvent.click(screen.getByRole("button", { name: "确认加入候选" }));

    expect(screen.getByText("触发价必须大于 0")).toBeTruthy();
    expect(addCandidateMutate).not.toHaveBeenCalled();
  });

  it("回踩区间下沿高于上沿时显示行内错误且不提交", async () => {
    vi.mocked(getChanStructure).mockResolvedValue(fullStructure());
    withClient(<ChanPage />);
    fireEvent.click(await screen.findByRole("button", { name: "加入候选" }));
    fireEvent.change(screen.getByLabelText("区间下沿"), { target: { value: "90.00" } });
    fireEvent.change(screen.getByLabelText("区间上沿"), { target: { value: "89.00" } });

    fireEvent.click(screen.getByRole("button", { name: "确认加入候选" }));

    expect(screen.getByText("区间下沿不能高于区间上沿")).toBeTruthy();
    expect(addCandidateMutate).not.toHaveBeenCalled();
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
