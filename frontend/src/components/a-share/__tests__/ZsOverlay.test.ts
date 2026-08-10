import { describe, it, expect } from "vitest";
import { ZsOverlay } from "../CandlestickChart";

/**
 * ZsOverlay（中枢矩形 ISeriesPrimitive）隔离结构测试。
 *
 * 设计 flag 该 primitive 为"最新颖代码，先 mock spike"。canvas draw 在 jsdom
 * 无法验证，这里覆盖无 canvas 依赖的结构契约：rects 持有 / paneViews 返回
 * renderer / attached-detached 生命周期。draw 本身留作 dev-server 肉眼验证。
 */
describe("ZsOverlay 结构契约", () => {
  it("setRects 持有矩形 + 触发 requestUpdate", () => {
    let updated = 0;
    const o = new ZsOverlay();
    o.attached({
      chart: {} as never,
      series: {} as never,
      horzScaleBehavior: {} as never,
      requestUpdate: () => {
        updated++;
      },
    });
    const rects = [
      { sdt: 1 as never, edt: 5 as never, zg: 90, zd: 80, state: "confirmed" as const },
      { sdt: 6 as never, edt: 9 as never, zg: 100, zd: 95, state: "extending" as const },
    ];
    o.setRects(rects);
    expect(o.getRects()).toHaveLength(2);
    expect(updated).toBe(1);
  });

  it("paneViews 返回含 renderer 的视图（可被 lightweight-charts 调用）", () => {
    const o = new ZsOverlay();
    const views = o.paneViews();
    expect(views).toHaveLength(1);
    const view = views[0];
    expect(view).toBeDefined();
    expect(typeof view!.renderer).toBe("function");
    const r = view!.renderer();
    expect(r).not.toBeNull();
    expect(typeof r!.draw).toBe("function"); // 无数据 draw 不抛
    expect(() => r!.draw({ useBitmapCoordinateSpace: () => {} } as never)).not.toThrow();
  });

  it("detached 清理引用", () => {
    const o = new ZsOverlay();
    o.attached({
      chart: {} as never,
      series: {} as never,
      horzScaleBehavior: {} as never,
      requestUpdate: () => {},
    });
    o.detached();
    // detached 后 draw 不抛（无 chart/series 直接 return）
    const view = o.paneViews()[0];
    expect(view).toBeDefined();
    const r = view!.renderer();
    expect(r).not.toBeNull();
    expect(() => r!.draw({ useBitmapCoordinateSpace: () => {} } as never)).not.toThrow();
  });
});
