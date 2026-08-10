import { describe, it, expect } from "vitest";
import {
  calcTodayPnlTotal,
  positionTodayPnl,
  closedTodayPnl,
} from "@/routes/overview/todayPnl";
import type { ActivePosition, ClosedPosition } from "@/api/watchlist";

const TODAY = "2026-07-30";

function makePos(overrides: Partial<ActivePosition>): ActivePosition {
  return {
    ts_code: "000001.SZ",
    name: "测试股",
    entry_price: 10,
    entry_date: "2026-07-01",
    trigger_price: null,
    trigger_direction: "below",
    expires_at: "2026-08-01",
    status: "active",
    position_size_shares: 100,
    ...overrides,
  };
}

function makeClosed(overrides: {
  entryDate?: string;
  fillPrice?: number | null;
  entryPrice?: number | null;
  shares?: number | null;
  exitPrice?: number;
}): ClosedPosition {
  return {
    ts_code: "000002.SZ",
    name: "已平仓",
    open: {
      entry_date: overrides.entryDate ?? "2026-07-01",
      entry_price: "entryPrice" in overrides ? overrides.entryPrice! : 10,
      actual_fill_price: "fillPrice" in overrides ? overrides.fillPrice! : 10,
      position_size_shares: "shares" in overrides ? overrides.shares! : 100,
    },
    close: {
      exit_date: TODAY,
      actual_exit_price: overrides.exitPrice ?? 11,
      exit_reason: "manual",
      realized_pnl_amount: null,
      realized_pnl_pct: null,
      closed_at: `${TODAY}T14:00:00`,
    },
    diagnosis: null,
  };
}

describe("positionTodayPnl", () => {
  it("早前开仓: (现价 - 昨收) × 股数", () => {
    const pos = makePos({ entry_date: "2026-07-01", position_size_shares: 400 });
    expect(positionTodayPnl(pos, 35.6, 35.74, TODAY)).toBeCloseTo(-56);
  });

  it("今日开仓: 基准是买入价, 不吃隔夜跳空(瑞芯微案例)", () => {
    // 昨收 203.05, 今日 185.2 买入, 现价 182.75 → -245, 不是 -2030
    const pos = makePos({
      ts_code: "603893.SH",
      entry_date: TODAY,
      entry_price: 185.2,
      position_size_shares: 100,
    });
    expect(positionTodayPnl(pos, 182.75, 203.05, TODAY)).toBeCloseTo(-245);
  });

  it("今日开仓且有 avg_cost: 用 avg_cost(盘中加仓后的成本基准)", () => {
    const pos = makePos({
      entry_date: TODAY,
      entry_price: 185.2,
      avg_cost: 184.0,
      position_size_shares: 100,
    });
    expect(positionTodayPnl(pos, 182.75, 203.05, TODAY)).toBeCloseTo(-125);
  });

  it("今日开仓: 昨收缺失也能算(基准不依赖昨收)", () => {
    const pos = makePos({ entry_date: TODAY, entry_price: 185.2 });
    expect(positionTodayPnl(pos, 182.75, null, TODAY)).toBeCloseTo(-245);
  });

  it("缺现价或缺股数 → null", () => {
    const pos = makePos({});
    expect(positionTodayPnl(pos, null, 35.74, TODAY)).toBeNull();
    expect(
      positionTodayPnl(makePos({ position_size_shares: undefined }), 35.6, 35.74, TODAY),
    ).toBeNull();
  });
});

describe("closedTodayPnl", () => {
  it("昨日开仓今日平仓: (exit - 昨收) × 股数", () => {
    const c = makeClosed({ entryDate: "2026-07-01", exitPrice: 11, shares: 100 });
    expect(closedTodayPnl(c, 10.5, TODAY)).toBeCloseTo(50);
  });

  it("今日开今日平(日内): (exit - 买入价) × 股数, 不用昨收", () => {
    const c = makeClosed({
      entryDate: TODAY,
      fillPrice: 100,
      exitPrice: 103,
      shares: 200,
    });
    // 昨收 95(隔夜跳空 +5), 日内赚的只有 (103-100)*200 = 600
    expect(closedTodayPnl(c, 95, TODAY)).toBeCloseTo(600);
  });

  it("日内平仓缺 actual_fill_price 时回退 entry_price", () => {
    const c = makeClosed({
      entryDate: TODAY,
      fillPrice: null,
      entryPrice: 100,
      exitPrice: 103,
      shares: 200,
    });
    expect(closedTodayPnl(c, 95, TODAY)).toBeCloseTo(600);
  });

  it("缺 exit/股数/基准 → null", () => {
    expect(closedTodayPnl(makeClosed({ shares: null }), 10, TODAY)).toBeNull();
    expect(closedTodayPnl(makeClosed({}), null, TODAY)).toBeNull();
  });
});

describe("calcTodayPnlTotal", () => {
  it("混合: 隔夜仓按昨收 + 今日新开仓按买入价", () => {
    const positions = [
      makePos({ ts_code: "002415.SZ", entry_date: "2026-06-27", position_size_shares: 400 }),
      makePos({ ts_code: "603893.SH", entry_date: TODAY, entry_price: 185.2, position_size_shares: 100 }),
    ];
    const total = calcTodayPnlTotal({
      positions,
      closedToday: [],
      prices: { "002415.SZ": 35.6, "603893.SH": 182.75 },
      prevClose: { "002415.SZ": 35.74, "603893.SH": 203.05 },
      closedPrevClose: {},
      todayStr: TODAY,
    });
    // -56 + (-245) = -301 (修复前是 -56 + -2030 = -2086)
    expect(total).toBeCloseTo(-301);
  });

  it("在仓腿任一缺数据 → 整体 null(沿用原语义)", () => {
    const total = calcTodayPnlTotal({
      positions: [makePos({})],
      closedToday: [],
      prices: {},
      prevClose: { "000001.SZ": 10 },
      closedPrevClose: {},
      todayStr: TODAY,
    });
    expect(total).toBeNull();
  });

  it("平仓腿缺昨收 → 跳过该条, 不拖垮在仓部分", () => {
    const total = calcTodayPnlTotal({
      positions: [makePos({ position_size_shares: 100 })],
      closedToday: [makeClosed({ entryDate: "2026-07-01", exitPrice: 11 })],
      prices: { "000001.SZ": 10.5 },
      prevClose: { "000001.SZ": 10 },
      closedPrevClose: {},
      todayStr: TODAY,
    });
    expect(total).toBeCloseTo(50);
  });

  it("无持仓无平仓 → null", () => {
    expect(
      calcTodayPnlTotal({
        positions: [],
        closedToday: [],
        prices: {},
        prevClose: {},
        closedPrevClose: {},
        todayStr: TODAY,
      }),
    ).toBeNull();
  });

  it("只有当日平仓时也能给出合计", () => {
    const total = calcTodayPnlTotal({
      positions: [],
      closedToday: [makeClosed({ entryDate: "2026-07-01", exitPrice: 11, shares: 100 })],
      prices: {},
      prevClose: {},
      closedPrevClose: { "000002.SZ": 10.5 },
      todayStr: TODAY,
    });
    expect(total).toBeCloseTo(50);
  });
});
