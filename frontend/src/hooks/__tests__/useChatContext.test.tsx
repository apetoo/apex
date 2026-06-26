import { describe, it, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { ChatContextProvider, useChatContext } from "../useChatContext";

/**
 * ChatContextProvider 单测(ED2 修正)
 *
 * 关键: 上下文按 hash 单次注入, 不每条 message prepend。
 *   - 首次 setContext + buildMessage: 上下文 + 用户消息拼接
 *   - 后续 buildMessage(同 context): 不重复 prepend
 *   - context 变化: 下次 buildMessage 重新 prepend
 */

function wrapper({ children }: { children: React.ReactNode }) {
  return <ChatContextProvider>{children}</ChatContextProvider>;
}

describe("useChatContext(ED2 单次注入)", () => {
  it("首次 buildMessage: 空上下文 → 仅用户消息", () => {
    const { result } = renderHook(() => useChatContext(), { wrapper });
    let msg = "";
    act(() => {
      msg = result.current.buildMessage("你好");
    });
    expect(msg).toBe("你好");
    expect(result.current.lastInjectedHash).not.toBeNull();
  });

  it("首次 buildMessage: 有上下文 → 上下文 + 用户消息拼接", () => {
    const { result } = renderHook(() => useChatContext(), { wrapper });
    act(() => {
      result.current.setContext("verdict: 看多");
    });
    const msg = result.current.buildMessage("能加仓吗");
    expect(msg).toContain("[当前上下文]");
    expect(msg).toContain("verdict: 看多");
    expect(msg).toContain("[用户问题]");
    expect(msg).toContain("能加仓吗");
  });

  it("ED2 关键: 同上下文不重复 prepend(history 不膨胀)", () => {
    const { result } = renderHook(() => useChatContext(), { wrapper });
    act(() => {
      result.current.setContext("verdict: 看多");
    });
    const first = result.current.buildMessage("第一问");
    const second = result.current.buildMessage("第二问");
    // 第一次有上下文前缀
    expect(first).toContain("[当前上下文]");
    // 第二次: hash 相同, 不重复 prepend, 仅用户消息
    expect(second).toBe("第二问");
  });

  it("context 变化: 下次 buildMessage 重新 prepend", () => {
    const { result } = renderHook(() => useChatContext(), { wrapper });
    act(() => {
      result.current.setContext("verdict: 看多");
    });
    result.current.buildMessage("第一问"); // 注入 hash A
    act(() => {
      result.current.setContext("verdict: 中性"); // 变化
    });
    const msg = result.current.buildMessage("第二问");
    // 重新注入了
    expect(msg).toContain("verdict: 中性");
    expect(msg).toContain("第二问");
  });

  it("setContext 后 context 值更新, 路由切换可重置", () => {
    const { result } = renderHook(() => useChatContext(), { wrapper });
    act(() => {
      result.current.setContext("持仓: 002466.SZ");
    });
    expect(result.current.context).toBe("持仓: 002466.SZ");
    act(() => {
      result.current.setContext("");
    });
    expect(result.current.context).toBe("");
    // context 清空后, buildMessage 应不 prepend
    const msg = result.current.buildMessage("hello");
    expect(msg).toBe("hello");
  });
});
