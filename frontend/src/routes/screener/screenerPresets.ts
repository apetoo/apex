import type { ScreenerWeights } from "@/api/screener";

export type ScreenerMode = "auto" | "steady" | "balanced" | "aggressive";
export type ManualScreenerMode = Exclude<ScreenerMode, "auto">;

export const SCREENER_MODE_OPTIONS: ReadonlyArray<{
  mode: ScreenerMode;
  label: string;
  description: string;
}> = [
  {
    mode: "auto",
    label: "自动适配（推荐）",
    description: "根据市场状态、历史表现和今日候选数自动分配",
  },
  { mode: "steady", label: "稳健波段", description: "偏资金确认、回踩与低吸" },
  { mode: "balanced", label: "均衡", description: "兼顾趋势、轮动与资金" },
  { mode: "aggressive", label: "进攻趋势", description: "偏首板、龙头与放量突破" },
];

export const SCREENER_PRESETS: Record<ManualScreenerMode, ScreenerWeights> = {
  steady: {
    first_board_leader: 0,
    institutional_flow: 0.25,
    industry_rotation: 0.1,
    leader_with_volume: 0,
    pullback_to_ma: 0.25,
    bullish_alignment: 0.1,
    volume_breakout: 0,
    stealth_accumulation: 0.3,
  },
  balanced: {
    first_board_leader: 0.1,
    institutional_flow: 0.15,
    industry_rotation: 0.15,
    leader_with_volume: 0.05,
    pullback_to_ma: 0.2,
    bullish_alignment: 0.15,
    volume_breakout: 0.1,
    stealth_accumulation: 0.1,
  },
  aggressive: {
    first_board_leader: 0.25,
    institutional_flow: 0,
    industry_rotation: 0.15,
    leader_with_volume: 0.2,
    pullback_to_ma: 0.05,
    bullish_alignment: 0.1,
    volume_breakout: 0.25,
    stealth_accumulation: 0,
  },
};

export function clonePreset(mode: ManualScreenerMode): ScreenerWeights {
  return { ...SCREENER_PRESETS[mode] };
}
