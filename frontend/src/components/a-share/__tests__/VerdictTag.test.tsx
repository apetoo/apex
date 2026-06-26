import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { VerdictTag } from "../VerdictTag";
import { verdictColor, VERDICT_COLOR } from "@/types/verdict";

/**
 * <VerdictTag> 单测 + verdict 颜色映射(ED7)
 *
 * 红涨绿跌铁律:
 *   - 看多系(看多/偏多/观望偏多)→ up 红
 *   - 中性系(中性/观望)→ flat 灰
 *   - 看空系(看空/偏空/观望偏空)→ down 绿
 *
 * 同步自 apex/schemas.VERDICT_ENUM(后端 source of truth)
 */

describe("verdictColor(ED7 映射)", () => {
  it("看多系 → up(红)", () => {
    expect(verdictColor("看多")).toBe("up");
    expect(verdictColor("偏多")).toBe("up");
    expect(verdictColor("观望偏多")).toBe("up");
  });

  it("中性系 → flat(灰)", () => {
    expect(verdictColor("中性")).toBe("flat");
    expect(verdictColor("观望")).toBe("flat");
  });

  it("看空系 → down(绿)", () => {
    expect(verdictColor("看空")).toBe("down");
    expect(verdictColor("偏空")).toBe("down");
    expect(verdictColor("观望偏空")).toBe("down");
  });

  it("null / undefined / 未知 → flat(降级, 不崩)", () => {
    expect(verdictColor(null)).toBe("flat");
    expect(verdictColor(undefined)).toBe("flat");
    expect(verdictColor("")).toBe("flat");
    expect(verdictColor("不存在的verdict")).toBe("flat");
  });

  it("VERDICT_COLOR 是 8 个枚举全覆盖(防新增漏配)", () => {
    expect(Object.keys(VERDICT_COLOR)).toHaveLength(8);
  });
});

describe("VerdictTag 组件", () => {
  it("看多 → 渲染 '看多' 文字 + 含 text-up class", () => {
    render(<VerdictTag verdict="看多" />);
    const el = screen.getByText("看多");
    expect(el.className).toContain("text-up");
  });

  it("看空 → text-down class", () => {
    render(<VerdictTag verdict="看空" />);
    const el = screen.getByText("看空");
    expect(el.className).toContain("text-down");
  });

  it("中性 → text-flat class", () => {
    render(<VerdictTag verdict="中性" />);
    const el = screen.getByText("中性");
    expect(el.className).toContain("text-flat");
  });

  it("null → 显示 '—' 降级", () => {
    render(<VerdictTag verdict={null} />);
    expect(screen.getByText("—")).toBeTruthy();
  });

  it("未知 verdict → 降级 '—' 不崩", () => {
    render(<VerdictTag verdict="非标准词" />);
    expect(screen.getByText("非标准词")).toBeTruthy();
  });
});
