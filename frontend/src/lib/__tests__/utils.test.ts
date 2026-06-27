import { describe, it, expect } from "vitest";
import {
  cn,
  directionClass,
  formatPrice,
  formatPercent,
  formatRatio,
  formatDelta,
  amountKToYi,
  formatVolume,
} from "../utils";

/**
 * 数字格式化单测(ED15 + 百分比/比率区分)
 *
 * 红涨绿跌铁律 + 区分"已是百分比值"vs"0-1 比率"两种语义。
 */

describe("formatPercent(已是百分比的数, 8.2 → '+8.20%')", () => {
  it("正数带 + 号", () => {
    expect(formatPercent(8.2)).toBe("+8.20%");
    expect(formatPercent(0.5)).toBe("+0.50%");
  });
  it("负数带 - 号(自带负号)", () => {
    expect(formatPercent(-3.1)).toBe("-3.10%");
  });
  it("0 显示无符号 0.00%", () => {
    expect(formatPercent(0)).toBe("0.00%");
  });
  it("null/undefined/NaN → —", () => {
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(undefined)).toBe("—");
    expect(formatPercent(NaN)).toBe("—");
  });
});

describe("formatRatio(0-1 比率, 0.6 → '60%')", () => {
  it("0-1 小数 → 百分比(默认 0 位)", () => {
    expect(formatRatio(0.6)).toBe("60%");
    expect(formatRatio(0.78)).toBe("78%");
    expect(formatRatio(1)).toBe("100%");
    expect(formatRatio(0)).toBe("0%");
  });
  it("可指定小数位", () => {
    expect(formatRatio(0.0161, 2)).toBe("1.61%");
    expect(formatRatio(0.87654, 2)).toBe("87.65%");
  });
  it("null/undefined/NaN → —", () => {
    expect(formatRatio(null)).toBe("—");
    expect(formatRatio(undefined)).toBe("—");
    expect(formatRatio(NaN)).toBe("—");
  });
});

describe("formatPrice(2 位小数等宽)", () => {
  it("正常数字", () => {
    expect(formatPrice(11.32)).toBe("11.32");
    expect(formatPrice(3245.67)).toBe("3245.67");
  });
  it("null → —", () => {
    expect(formatPrice(null)).toBe("—");
    expect(formatPrice(NaN)).toBe("—");
  });
});

describe("formatDelta(涨跌额带正负号)", () => {
  it("正数 + 负数带符号", () => {
    expect(formatDelta(0.27)).toBe("+0.27");
    expect(formatDelta(-0.55)).toBe("-0.55");
  });
  it("0 → 无符号 0.00", () => {
    expect(formatDelta(0)).toBe("0.00");
  });
});

describe("directionClass(红涨绿跌)", () => {
  it("正 → text-up(红)", () => {
    expect(directionClass(1)).toBe("text-up");
    expect(directionClass(0.01)).toBe("text-up");
  });
  it("负 → text-down(绿)", () => {
    expect(directionClass(-1)).toBe("text-down");
  });
  it("0 / null / undefined / NaN → text-flat(灰)", () => {
    expect(directionClass(0)).toBe("text-flat");
    expect(directionClass(null)).toBe("text-flat");
    expect(directionClass(undefined)).toBe("text-flat");
    expect(directionClass(NaN)).toBe("text-flat");
  });
});

describe("cn(className 合并, tailwind-merge 去重)", () => {
  it("去重冲突的 tailwind class", () => {
    expect(cn("px-2", "px-4")).toBe("px-4");
    expect(cn("text-flat", "text-up")).toBe("text-up");
  });
  it("保留非冲突", () => {
    expect(cn("text-up", "rounded")).toBe("text-up rounded");
  });
  it("支持条件", () => {
    expect(cn("base", false && "hidden", "end")).toBe("base end");
  });
});

describe("amountKToYi(千元 → 亿元, ÷1e4)", () => {
  it("6.5e7 千元 → 6500 亿", () => {
    expect(amountKToYi(6.5e7)).toBe(6500);
  });
  it("1e4 千元 → 1 亿", () => {
    expect(amountKToYi(1e4)).toBe(1);
  });
  it("0 → 0", () => {
    expect(amountKToYi(0)).toBe(0);
  });
  it("null/undefined/NaN → null", () => {
    expect(amountKToYi(null)).toBeNull();
    expect(amountKToYi(undefined)).toBeNull();
    expect(amountKToYi(NaN)).toBeNull();
  });
});

describe("formatVolume(亿元 → 显示串)", () => {
  it("≥1e4 亿 → X.XX 万亿", () => {
    expect(formatVolume(12345)).toBe("1.23 万亿");
    expect(formatVolume(14000)).toBe("1.40 万亿");
    expect(formatVolume(10000)).toBe("1.00 万亿");
  });
  it("<1e4 亿 → XXXX 亿(整数)", () => {
    expect(formatVolume(1234.56)).toBe("1235 亿");
    expect(formatVolume(9999)).toBe("9999 亿");
    expect(formatVolume(0)).toBe("0 亿");
  });
  it("null/undefined/NaN → —", () => {
    expect(formatVolume(null)).toBe("—");
    expect(formatVolume(undefined)).toBe("—");
    expect(formatVolume(NaN)).toBe("—");
  });
});
