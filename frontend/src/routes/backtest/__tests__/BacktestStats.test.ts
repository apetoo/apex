import { describe, expect, it } from "vitest";

import { computeStats, getSignalOutcome } from "../stats";

describe("backtest official statistics", () => {
  it("excludes pending and non-fill signals from win rate and averages", () => {
    const stats = computeStats([
      { status: "completed", net_return: 0.1, hit: true },
      { status: "data_truncated", net_return: -0.05, hit: false },
      { status: "pending", net_return: null, hit: null },
      { status: "unfillable", net_return: null, hit: null },
    ]);

    expect(stats.count).toBe(2);
    expect(stats.pendingCount).toBe(1);
    expect(stats.unfillableCount).toBe(1);
    expect(stats.winRate).toBe(0.5);
    expect(stats.avgNet).toBeCloseTo(0.025);
  });

  it("presents pending signals without a win or loss outcome", () => {
    expect(getSignalOutcome({ status: "pending", hit: null })).toEqual({
      label: "进行中",
      tone: "flat",
    });
  });

  it("does not present zero win rate when every signal is pending", () => {
    const stats = computeStats([
      { status: "pending", net_return: null, hit: null },
    ]);

    expect(stats.winRate).toBeNull();
    expect(stats.avgNet).toBeNull();
  });
});
