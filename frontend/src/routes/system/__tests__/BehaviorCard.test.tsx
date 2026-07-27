import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { BehaviorCard } from "../SystemPage";
import type { SystemView } from "@/api/system";

const noDataRate = { n: 0, count: 0, rate: null, confidence: "no_data" as const };

function makeData(pa?: SystemView["behavior"]["position_action_adherence"]) {
  return {
    behavior: {
      chase: noDataRate,
      average_down: noDataRate,
      stop_discipline: {
        n: 0, no_stop_n: 0, honored_n: 0, override_breach_n: 0, not_triggered_n: 0,
        confidence: "no_data",
      },
      take_profit_discipline: {
        n: 0, no_target_n: 0, honored_n: 0, missed_n: 0, not_hit_n: 0,
        confidence: "no_data",
      },
      proxy_emotional: { revenge: noDataRate, fomo: noDataRate, overtrading: noDataRate },
      position_action_adherence: pa,
    },
  } as unknown as SystemView;
}

describe("BehaviorCard 加减仓 adherence", () => {
  it("有数据 -> 渲染 follow_rate + 全桶标签 + 桶计数", () => {
    const pa = {
      follow_n: 2, deviate_n: 1, partial_n: 0, na_n: 3, null_n: 5, stale_n: 1,
      actionable_n: 3, n: 2, follow_rate: 0.5, confidence: "low_sample_descriptive_only",
    } as SystemView["behavior"]["position_action_adherence"];
    render(<BehaviorCard data={makeData(pa)} />);

    expect(screen.getByText("加减仓守规")).toBeInTheDocument();
    expect(screen.getByText("对比 AI 加减仓建议")).toBeInTheDocument();
    expect(screen.getByText("50%")).toBeInTheDocument(); // follow_rate
    // 全桶标签
    for (const label of ["跟建议", "偏离", "部分减仓", "不适用", "无建议", "陈旧(>14d)"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    // 桶计数数据流（follow_n=2 唯一）
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("no_data -> 无 follow_rate 百分比，显示无数据徽章", () => {
    const pa = {
      follow_n: 0, deviate_n: 0, partial_n: 0, na_n: 0, null_n: 8, stale_n: 0,
      actionable_n: 0, n: 0, follow_rate: null, confidence: "no_data",
    } as SystemView["behavior"]["position_action_adherence"];
    render(<BehaviorCard data={makeData(pa)} />);

    // 全卡 no_data -> 多个无数据徽章；adherence 区块也渲染
    expect(screen.getAllByText(/无数据/).length).toBeGreaterThan(0);
    expect(screen.getByText("加减仓守规")).toBeInTheDocument();
    expect(screen.queryByText("50%")).not.toBeInTheDocument();
  });

  it("pa 缺失（旧 system.json stale cache）-> 不崩，退化为无数据", () => {
    render(<BehaviorCard data={makeData(undefined)} />);
    expect(screen.getByText("加减仓守规")).toBeInTheDocument();
    expect(screen.getAllByText(/无数据/).length).toBeGreaterThan(0);
    expect(screen.queryByText("50%")).not.toBeInTheDocument();
  });
});
