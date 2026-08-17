import { describe, expect, it } from "vitest";

import { SCREENER_PRESETS } from "../screenerPresets";

const STRATEGIES = [
  "first_board_leader",
  "institutional_flow",
  "industry_rotation",
  "leader_with_volume",
  "pullback_to_ma",
  "bullish_alignment",
  "volume_breakout",
  "stealth_accumulation",
];

describe("screener strategy presets", () => {
  it.each(["steady", "balanced", "aggressive"] as const)(
    "%s covers every strategy and totals one",
    (mode) => {
      expect(Object.keys(SCREENER_PRESETS[mode])).toEqual(STRATEGIES);
      expect(Object.values(SCREENER_PRESETS[mode]).reduce((sum, weight) => sum + weight, 0)).toBeCloseTo(1);
    },
  );

  it("keeps the approved style anchors", () => {
    expect(SCREENER_PRESETS.steady.stealth_accumulation).toBe(0.3);
    expect(SCREENER_PRESETS.balanced.pullback_to_ma).toBe(0.2);
    expect(SCREENER_PRESETS.aggressive.volume_breakout).toBe(0.25);
  });
});
