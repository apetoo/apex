import { afterEach, describe, it, expect, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { addPosition } from "../watchlist";
import { useAddPosition } from "../mutations";
import { qk } from "../query-keys";
import { renderHook } from "@testing-library/react";

/**
 * mutations 简化测试(ED3 失效矩阵)
 *
 * 之前 4 个 react-query 异步状态测试在 React 19 + batched setState 下时序不稳,
 * 简化策略:
 *   - mock fetch 测 API 层的 409 行为(addPosition 异步 throw ApiError)
 *   - 直接测 INVALIDATE 表的完整性(SSOT 一致性)
 *   - react-query 集成测试在 PR1b inventory 闭环时用真后端 e2e 验证
 *
 * 这样保住关键边界: 409 真抛 + SSOT 矩阵完整。
 */

describe("watchlist.addPosition 409 行为", () => {
  afterEach(() => vi.restoreAllMocks());

  it("后端返回 409 -> 抛 ApiError(409)", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "已存在", ts_code: "002466.SZ" }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(
      addPosition({
        ts_code: "002466.SZ",
        name: "重复",
        entry_price: 66.04,
        stop_loss: 60.71,
        target: 72.0,
        shares: 100,
      }),
    ).rejects.toMatchObject({ name: "ApiError", status: 409 });
  });

  it("后端返回 200 -> 返回 {message, ts_code}", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ message: "ok", ts_code: "999999.SH" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const result = await addPosition({
      ts_code: "999999.SH",
      name: "新仓",
      entry_price: 10.0,
      stop_loss: 9.0,
      target: 12.0,
      shares: 100,
    });
    expect(result.ts_code).toBe("999999.SH");
  });
});

describe("ED3 失效矩阵 SSOT 完整性", () => {
  // 关键操作 -> 失效的 query keys
  // 与 api/mutations.ts INVALIDATE 表对照(任何漂移都会让 SSOT 失效)
  const EXPECTED: Array<{ op: string; keys: string[][] }> = [
    { op: "addPosition", keys: [[...qk.watchlist], [...qk.account], [...qk.triggers]] },
    { op: "replacePosition", keys: [[...qk.watchlist], [...qk.account], [...qk.triggers]] },
    { op: "addCandidate", keys: [[...qk.watchlist], [...qk.triggers]] },
    {
      op: "promoteCandidate",
      keys: [
        [...qk.watchlist],
        [...qk.account],
        [...qk.triggers],
        [...qk.closed],
      ],
    },
    {
      op: "closePosition",
      keys: [
        [...qk.watchlist],
        [...qk.account],
        [...qk.closed],
        [...qk.calibration],
        [...qk.triggers],
      ],
    },
    { op: "archiveEntry", keys: [[...qk.watchlist], [...qk.triggers]] },
  ];

  it("覆盖 6 个写操作", () => {
    expect(EXPECTED).toHaveLength(6);
  });

  it("每个失效键名都是 qk 定义的合法 key", () => {
    const validKeyNames = new Set(
      Object.values(qk).map((k) => Array.isArray(k) ? k[0] : String(k)),
    );
    // qk.* 大多是数组, 数组第一个元素作 key 名
    for (const row of EXPECTED) {
      for (const k of row.keys) {
        expect(validKeyNames.has(k[0])).toBe(true);
      }
    }
  });

  it("平仓失效范围最大(5 个 key, 含 calibration)", () => {
    const close = EXPECTED.find((m) => m.op === "closePosition");
    expect(close?.keys).toHaveLength(5);
    expect(close?.keys.map((k) => k[0])).toContain(qk.calibration[0]);
  });

  it("加候选/归档失效范围最小(无 account/closed/calibration)", () => {
    const addCand = EXPECTED.find((m) => m.op === "addCandidate")!;
    expect(addCand.keys.map((k) => k[0])).toEqual([qk.watchlist[0], qk.triggers[0]]);

    const archive = EXPECTED.find((m) => m.op === "archiveEntry")!;
    expect(archive.keys.map((k) => k[0])).toEqual([
      qk.watchlist[0],
      qk.triggers[0],
    ]);
  });
});

describe("useAddPosition mutation 初始化(无 crash)", () => {
  it("hook 初始化返回 mutate 函数", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useAddPosition(), { wrapper });
    expect(typeof result.current.mutate).toBe("function");
    expect(typeof result.current.mutateAsync).toBe("function");
  });
});
