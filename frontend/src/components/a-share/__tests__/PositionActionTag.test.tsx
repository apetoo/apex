import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { PositionActionTag } from "../PositionActionTag";

describe("PositionActionTag", () => {
  it("hold/add/trim/exit -> 对应标签 + 红涨绿跌色调", () => {
    const { rerender } = render(<PositionActionTag action="hold" />);
    expect(screen.getByText("持有").className).toContain("text-text-primary");
    rerender(<PositionActionTag action="add" />);
    expect(screen.getByText("加仓").className).toContain("text-up");
    rerender(<PositionActionTag action="trim" />);
    expect(screen.getByText("减仓").className).toContain("text-down");
    rerender(<PositionActionTag action="exit" />);
    expect(screen.getByText("清仓").className).toContain("text-down");
  });

  it("未知 action -> 回退显示原文本（灰）", () => {
    render(<PositionActionTag action="whatever" />);
    expect(screen.getByText("whatever").className).toContain("text-flat");
  });

  it("空 action -> 显示'持仓建议'", () => {
    render(<PositionActionTag action="" />);
    expect(screen.getByText("持仓建议")).toBeTruthy();
  });
});
