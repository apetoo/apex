import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { PriceTag } from "../PriceTag";

/**
 * <PriceTag> 关键逻辑单测(ED8)
 *
 * 红涨绿跌铁律 + ED15 四态(loading/error/empty/盘外)
 */

describe("PriceTag", () => {
  it("涨: price > prevClose → 红色(up) + 正涨跌额/幅", () => {
    render(<PriceTag price={10.5} prevClose={10.0} />);
    const priceEl = screen.getByText("10.50");
    // tailwind class text-up = 红
    expect(priceEl.className).toContain("text-up");
    // 涨跌额 +0.50, 涨幅 +5.00%
    expect(screen.getByText(/\+0\.50/)).toBeTruthy();
    expect(screen.getByText(/\+5\.00%/)).toBeTruthy();
  });

  it("跌: price < prevClose → 绿色(down) + 负涨跌额/幅", () => {
    render(<PriceTag price={9.5} prevClose={10.0} />);
    const priceEl = screen.getByText("9.50");
    expect(priceEl.className).toContain("text-down");
    expect(screen.getByText(/-0\.50/)).toBeTruthy();
    expect(screen.getByText(/-5\.00%/)).toBeTruthy();
  });

  it("平: price == prevClose 且非盘外判定 → flat(灰)", () => {
    // price === prevClose 时 isOutside=true, 但 delta=0 → directionClass 返回 flat
    render(<PriceTag price={10.0} prevClose={10.0} />);
    const priceEl = screen.getByText("10.00");
    expect(priceEl.className).toContain("text-flat");
  });

  it("盘外态: price === prevClose → 显示「盘外·昨收」标识", () => {
    render(<PriceTag price={10.0} prevClose={10.0} />);
    expect(screen.getByText("盘外·昨收")).toBeTruthy();
  });

  it("停牌: price 为 null → 显示「停牌」", () => {
    render(<PriceTag price={null} prevClose={10.0} />);
    expect(screen.getByText("停牌")).toBeTruthy();
  });

  it("loading 态: 显示骨架(无价格文字)", () => {
    const { container } = render(
      <PriceTag price={null} prevClose={null} loading />,
    );
    // 骨架是 animate-pulse div, 不应出现价格或停牌
    expect(container.querySelector(".animate-pulse")).toBeTruthy();
    expect(screen.queryByText("停牌")).toBeNull();
  });

  it("error 态: 显示「行情异常」", () => {
    render(<PriceTag price={null} prevClose={null} error />);
    expect(screen.getByText("行情异常")).toBeTruthy();
  });

  it("showChange=false: 不显示涨跌额/幅", () => {
    render(<PriceTag price={10.5} prevClose={10.0} showChange={false} />);
    expect(screen.getByText("10.50")).toBeTruthy();
    expect(screen.queryByText(/\+0\.50/)).toBeNull();
  });
});
