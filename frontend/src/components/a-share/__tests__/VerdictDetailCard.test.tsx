import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { VerdictDetailCard } from "../VerdictDetailCard";

/**
 * <VerdictDetailCard> Playstyle Engine v1 渲染单测（T7/T8）。
 *
 * 覆盖：完整星级条 + 主玩法标记 + 原因 + risk/fit 徽章；risk high=amber/low=flat；
 * playstyle=null(FE<0.5) -> 特征不足提示；老 entry(全 null) -> 不渲染玩法判定；
 * 星级按 ratings 填充；low_confidence 提示。
 *
 * AddCandidateDialog 用 useAddCandidate(react-query mutation)，mock 掉避免网络/QueryClient 依赖。
 */
vi.mock("@/api/mutations", () => ({
  useAddCandidate: () => ({ mutate: vi.fn(), isPending: false }),
  useReplacePosition: () => ({ mutate: vi.fn(), isPending: false }),
}));

function withClient(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const baseVerdict = {
  ts_code: "603019.SH",
  verdict: "偏多",
  confidence: 7,
  price_advice: { entry: 10, stop_loss: 9, target: 12 },
  evidence: [],
};

describe("VerdictDetailCard - Playstyle Engine v1 渲染", () => {
  it("完整 playstyle -> 星级条 + 主玩法标记 + 原因 + risk/fit 徽章", () => {
    withClient(
      <VerdictDetailCard
        verdict={{
          ...baseVerdict,
          playstyle: {
            ratings: { 打野: 4, 波段: 1, 中线: 0, 长线: 0 },
            primary: "打野",
            reasons: ["游资属性(近10日最高3连板)", "情绪驱动(换手18%>15%)"],
            low_confidence: false,
          },
          playstyle_fit: { state: "insufficient_data", note: "画像累积中（n<20）" },
          risk_level: "high",
        }}
      />,
    );
    expect(screen.getByText("玩法判定")).toBeTruthy();
    expect(screen.getByText("打野")).toBeTruthy();
    expect(screen.getByText("主玩法")).toBeTruthy(); // top 标记
    expect(screen.getByText(/游资属性/)).toBeTruthy(); // 原因
    // risk high -> amber
    const risk = screen.getByText("风险高");
    expect(risk.className).toContain("text-amber-600");
    // fit -> 画像累积中
    expect(screen.getByText(/画像累积中/)).toBeTruthy();
  });

  it("risk low -> 灰(flat)，非 amber", () => {
    withClient(
      <VerdictDetailCard
        verdict={{
          ...baseVerdict,
          playstyle: { ratings: { 打野: 0, 波段: 5, 中线: 0, 长线: 0 }, primary: "波段", reasons: [] },
          playstyle_fit: { state: "insufficient_data", note: "画像累积中" },
          risk_level: "low",
        }}
      />,
    );
    const risk = screen.getByText("风险低");
    expect(risk.className).toContain("text-flat");
    expect(risk.className).not.toContain("amber");
  });

  it("risk medium -> 灰(flat)", () => {
    withClient(
      <VerdictDetailCard
        verdict={{
          ...baseVerdict,
          playstyle: { ratings: { 打野: 0, 波段: 0, 中线: 4, 长线: 0 }, primary: "中线", reasons: [] },
          playstyle_fit: { state: "insufficient_data", note: "画像累积中" },
          risk_level: "medium",
        }}
      />,
    );
    expect(screen.getByText("风险中").className).toContain("text-flat");
  });

  it("playstyle=null(FE<0.5) -> 渲染'玩法特征不足' + fit 特征不足徽章，无星级", () => {
    withClient(
      <VerdictDetailCard
        verdict={{
          ...baseVerdict,
          playstyle: null,
          playstyle_fit: { state: "insufficient_data", note: "特征不足（FE 完整度<0.5）" },
          risk_level: "medium",
        }}
      />,
    );
    expect(screen.getByText(/玩法特征不足/)).toBeTruthy();
    expect(screen.getByText(/契合.*特征不足/)).toBeTruthy(); // fit 徽章
    expect(screen.queryByText("主玩法")).toBeNull();
  });

  it("老 entry(playstyle/fit/risk 全 null) -> 不渲染玩法判定", () => {
    withClient(
      <VerdictDetailCard
        verdict={{ ...baseVerdict, playstyle: null, playstyle_fit: null, risk_level: null }}
      />,
    );
    expect(screen.queryByText("玩法判定")).toBeNull();
  });

  it("星级条按 ratings 渲染填充星（5★ 全满）", () => {
    withClient(
      <VerdictDetailCard
        verdict={{
          ...baseVerdict,
          playstyle: { ratings: { 打野: 0, 波段: 5, 中线: 0, 长线: 0 }, primary: "波段", reasons: [] },
          playstyle_fit: { state: "insufficient_data", note: "画像累积中" },
        }}
      />,
    );
    // 波段行：name + 5 颗 ★ + 主玩法 标
    const bandRow = screen.getByText("波段").closest("div");
    expect(bandRow?.textContent).toContain("★★★★★");
    // 打野 0★ -> 该行不应含满星
    const dayeRow = screen.getByText("打野").closest("div");
    expect(dayeRow?.textContent).not.toContain("★");
  });

  it("low_confidence -> 显示'仅供参考'提示", () => {
    withClient(
      <VerdictDetailCard
        verdict={{
          ...baseVerdict,
          playstyle: {
            ratings: { 打野: 1, 波段: 0, 中线: 0, 长线: 0 },
            primary: "打野",
            reasons: [],
            low_confidence: true,
          },
          playstyle_fit: { state: "insufficient_data", note: "画像累积中" },
        }}
      />,
    );
    expect(screen.getByText(/仅供参考/)).toBeTruthy();
  });

  it("secondary 兼容玩法 -> 渲染'兼容'标（非 primary 行）", () => {
    withClient(
      <VerdictDetailCard
        verdict={{
          ...baseVerdict,
          playstyle: {
            ratings: { 打野: 0, 波段: 5, 中线: 4, 长线: 0 },
            primary: "波段",
            secondary: "中线",
            reasons: [],
          },
          playstyle_fit: { state: "insufficient_data", note: "画像累积中" },
        }}
      />,
    );
    // 波段是 primary -> 主玩法标；中线是 secondary -> 兼容标
    expect(screen.getByText("主玩法")).toBeTruthy();
    expect(screen.getByText("兼容")).toBeTruthy();
  });
});
