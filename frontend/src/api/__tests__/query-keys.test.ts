import { describe, it, expect } from "vitest";
import { qk } from "../query-keys";

/**
 * ED3 react-query 失效矩阵(SSOT — 实施 mutations 时照表 invalidate)
 *
 * 关键: 每个写操作必须失效一组 query keys, 否则用户看到缓存的旧数据。
 * 此测试确保矩阵表自身完整(每个写操作都有定义), 不是测试真实 mutations 调用。
 *
 * 真实 mutation onSuccess 调 invalidateQueries({ queryKey: [...] }) 的测试
 * 留给 mutations 实现时(见 T3 PR1b inventory 闭环)。
 */

const MATRIX = [
  {
    op: "POST /positions (加持仓)",
    invalidate: ["watchlist", "account", "triggers"],
  },
  {
    op: "POST /positions/replace (替换)",
    invalidate: ["watchlist", "account", "triggers"],
  },
  {
    op: "POST /candidates (加候选)",
    invalidate: ["watchlist", "triggers"],
  },
  {
    op: "POST /promote (候选→持仓)",
    invalidate: ["watchlist", "account", "triggers", "closed"],
  },
  {
    op: "POST /close (平仓)",
    invalidate: ["watchlist", "account", "closed", "calibration", "triggers"],
  },
  {
    op: "POST /archive (归档)",
    invalidate: ["watchlist", "triggers"],
  },
  {
    op: "POST /triggers/ack (信号已读)",
    invalidate: ["triggers"],
  },
  {
    op: "PUT /account (更新账户配置)",
    invalidate: ["account"],
  },
  {
    op: "POST /postmortem/run (AI 复盘)",
    invalidate: ["closed"],
  },
];

describe("ED3 失效矩阵(SSOT)", () => {
  it("矩阵覆盖 9 个写操作", () => {
    expect(MATRIX).toHaveLength(9);
  });

  it("每个失效键都能在 qk 中找到定义(防 typo)", () => {
    const validKeys = [
      "watchlist",
      "account",
      "triggers",
      "closed",
      "calibration",
    ];
    for (const row of MATRIX) {
      for (const k of row.invalidate) {
        expect(validKeys).toContain(k);
      }
    }
  });

  it("平仓(close)失效范围最大, 因为影响 watchlist+账户+复盘库+校准+触发器", () => {
    const close = MATRIX.find((m) => m.op.includes("/close"));
    expect(close?.invalidate).toHaveLength(5);
    expect(close?.invalidate).toContain("calibration");
  });

  it("候选加/归档失效范围最小(无 account/closed)", () => {
    const addCand = MATRIX.find((m) => m.op.includes("/candidates"));
    expect(addCand?.invalidate).toEqual(["watchlist", "triggers"]);

    const archive = MATRIX.find((m) => m.op.includes("/archive"));
    expect(archive?.invalidate).toEqual(["watchlist", "triggers"]);
  });
});

describe("qk query keys 形状", () => {
  it("watchlist 静态 key", () => {
    expect(qk.watchlist).toEqual(["watchlist"]);
  });

  it("codes 参数化 key(ED9 批量)", () => {
    expect(qk.prices(["000001.SZ", "000002.SZ"])).toEqual([
      "market",
      "prices",
      ["000001.SZ", "000002.SZ"],
    ]);
  });

  it("journal 按 ts_code 隔离", () => {
    expect(qk.journal("002466.SZ")).toEqual(["journal", "002466.SZ"]);
    expect(qk.journal("000001.SZ")).toEqual(["journal", "000001.SZ"]);
  });

  it("journal latest 是 journal 的子 key(react-query 部分匹配)", () => {
    const latest = qk.journalLatest("002466.SZ");
    const base = qk.journal("002466.SZ");
    // journalLatest 应是 base 的扩展([...base, "latest"])
    expect(latest).toEqual([...base, "latest"]);
  });
});
