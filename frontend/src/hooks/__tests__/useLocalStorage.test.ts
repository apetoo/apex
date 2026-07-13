import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useLocalStorage } from "../useLocalStorage";

/**
 * useLocalStorage 单测
 *   - 初始/读取/写入/函数式更新
 *   - 脏数据容错
 *   - 重新 mount 读到上次写入(持久化语义)
 */

describe("useLocalStorage", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("初始: localStorage 无值 -> 回退 initialValue", () => {
    const { result } = renderHook(() => useLocalStorage("k", [] as string[]));
    expect(result.current[0]).toEqual([]);
  });

  it("读取: localStorage 有值 -> 反序列化", () => {
    localStorage.setItem("k", JSON.stringify(["a", "b"]));
    const { result } = renderHook(() => useLocalStorage("k", [] as string[]));
    expect(result.current[0]).toEqual(["a", "b"]);
  });

  it("写入: setState 后同步到 localStorage", () => {
    const { result } = renderHook(() => useLocalStorage("k", [] as string[]));
    act(() => {
      result.current[1]((prev) => [...prev, "x"]);
    });
    expect(result.current[0]).toEqual(["x"]);
    expect(JSON.parse(localStorage.getItem("k")!)).toEqual(["x"]);
  });

  it("函数式更新: 基于前值累加", () => {
    const { result } = renderHook(() => useLocalStorage("k", 0));
    act(() => result.current[1]((n) => n + 1));
    act(() => result.current[1]((n) => n + 1));
    expect(result.current[0]).toBe(2);
    expect(JSON.parse(localStorage.getItem("k")!)).toBe(2);
  });

  it("容错: 脏 JSON -> 回退 initialValue 不抛", () => {
    localStorage.setItem("k", "{不是合法json");
    const { result } = renderHook(() => useLocalStorage("k", [] as string[]));
    expect(result.current[0]).toEqual([]);
  });

  it("持久化: unmount 后重新 mount 读到上次写入", () => {
    const { result: first, unmount } = renderHook(() =>
      useLocalStorage("k", [] as string[]),
    );
    act(() => first.current[1](["持久"]));
    unmount();

    const { result: second } = renderHook(() => useLocalStorage("k", [] as string[]));
    expect(second.current[0]).toEqual(["持久"]);
  });
});
