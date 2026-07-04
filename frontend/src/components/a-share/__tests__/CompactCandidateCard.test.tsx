import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { CompactCandidateCard } from "../CompactCandidateCard";
import type { Candidate } from "@/api/watchlist";

// mock 行情: 当前价/昨收可逐用例覆盖
const mockPrice: Record<string, number> = {};
const mockDaily: Record<string, number | undefined> = {};
vi.mock("@/api/market", () => ({
  getPrices: vi.fn(async (codes: string[]) =>
    Object.fromEntries(codes.map((c) => [c, mockPrice[c] ?? 22.15]))),
  getDailyPrices: vi.fn(async (codes: string[]) =>
    Object.fromEntries(codes.map((c) => [c, mockDaily[c]]))),
}));

function withClient(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const base: Candidate = {
  ts_code: "601012.SH",
  name: "隆基绿能",
  trigger_price: 22.5,
  trigger_direction: "below",
  expires_at: "2026-12-31",
  note: "",
  stop_advice: 21.0,
  target_advice: 25.0,
};

describe("CompactCandidateCard", () => {
  it("未触发 → 距触发行灰字(text-text-secondary)", async () => {
    mockPrice["601012.SH"] = 23.0; // 高于触发价 22.5, below 未触发, |pct|>2
    withClient(<CompactCandidateCard candidate={base} />);
    expect(screen.getByText("隆基绿能")).toBeTruthy();
    const distEl = await screen.findByText(/距触发/);
    expect(distEl.className).toContain("text-text-secondary");
    expect(screen.queryByText("已触发")).toBeNull();
  });

  it("接近触发(|pct|<2) → 距触发行黄字(text-amber-600)", async () => {
    mockPrice["601012.SH"] = 22.8; // (22.8-22.5)/22.5 = +1.33% → 高于触发价(below 未触发)且 |pct|<2 → 接近
    withClient(<CompactCandidateCard candidate={base} />);
    const distEl = await screen.findByText(/距触发/);
    expect(distEl.className).toContain("text-amber-600");
  });

  it("已触发 → 卡片浅红底 + 已触发红字, 不显示距触发", async () => {
    mockPrice["601012.SH"] = 22.0; // <= 22.5 → below 触发
    const { container } = render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <CompactCandidateCard candidate={base} />
      </QueryClientProvider>,
    );
    // 卡片根 div 带 bg-up/5
    await screen.findByText("已触发");
    const cardRoot = container.firstChild as HTMLElement;
    expect(cardRoot.className).toContain("bg-up/5");
    expect(screen.queryByText(/距触发/)).toBeNull();
  });

  it("渲染触发价/方向 + 建议止损/目标", async () => {
    mockPrice["601012.SH"] = 23.0;
    withClient(<CompactCandidateCard candidate={base} />);
    await screen.findByText(/距触发/);
    expect(screen.getByText(/22\.50/)).toBeTruthy(); // 触发价
    expect(screen.getByText(/下方/)).toBeTruthy();
    expect(screen.getByText("21.00")).toBeTruthy(); // 建议止损
    expect(screen.getByText("25.00")).toBeTruthy(); // 目标
  });

  it("有昨收 → 显示当日涨跌额/幅(红涨)", async () => {
    mockPrice["601012.SH"] = 23.0;
    mockDaily["601012.SH"] = 22.0; // 昨收 22 → +1.00 +4.55%
    withClient(<CompactCandidateCard candidate={base} />);
    const changeEl = await screen.findByText(/^\+1\.00/);
    expect(changeEl.textContent).toContain("+1.00");
    expect(changeEl.textContent).toContain("+4.55%");
    expect(changeEl.className).toContain("text-up"); // 红涨
  });

  it("无昨收 → 不显示涨跌(只显当前价)", async () => {
    mockPrice["601012.SH"] = 23.0;
    delete mockDaily["601012.SH"];
    withClient(<CompactCandidateCard candidate={base} />);
    await screen.findByText(/距触发/);
    expect(screen.queryByText(/^\+1\.00/)).toBeNull();
  });

  // ── 过期归因 meta ──────────────────────────────────────────────────────────

  // 用相对今天构造 expires_at, 避免硬编码日期漂移
  const isoDaysFromNow = (n: number) => {
    const d = new Date();
    d.setDate(d.getDate() + n);
    return d.toISOString().slice(0, 10);
  };

  it("vol_based meta → 显示剩余天数 + 「·波动率算」tag + tooltip 含 σ", async () => {
    mockPrice["601012.SH"] = 23.0;
    const cand: Candidate = {
      ...base,
      expires_at: isoDaysFromNow(10),
      expires_meta: { expires_days: 10, method: "vol_based", realized_vol: 0.0313, distance_pct: 5.2, t_trading: 2.99 },
    };
    const { container } = withClient(<CompactCandidateCard candidate={cand} />);
    await screen.findByText(/距触发/);
    expect(screen.getByText(/剩 10 天/)).toBeTruthy();
    expect(screen.getByText("·波动率算")).toBeTruthy();
    // tooltip 含 σ 与距离
    const expiringEl = container.querySelector('[title*="波动率算出"]');
    expect(expiringEl?.getAttribute("title")).toContain("σ3.13%");
    expect(expiringEl?.getAttribute("title")).toContain("距离5.20%");
  });

  it("manual meta → 显示「·手动」tag", async () => {
    mockPrice["601012.SH"] = 23.0;
    const cand: Candidate = {
      ...base,
      expires_at: isoDaysFromNow(14),
      expires_meta: { expires_days: 14, method: "manual" },
    };
    withClient(<CompactCandidateCard candidate={cand} />);
    await screen.findByText(/距触发/);
    expect(screen.getByText(/剩 14 天/)).toBeTruthy();
    expect(screen.getByText("·手动")).toBeTruthy();
  });

  it("即将过期(≤2天) → 剩余天数行黄字", async () => {
    mockPrice["601012.SH"] = 23.0;
    const cand: Candidate = {
      ...base,
      expires_at: isoDaysFromNow(1),
      expires_meta: { expires_days: 1, method: "vol_based", realized_vol: 0.05, distance_pct: 1, t_trading: 0.04 },
    };
    withClient(<CompactCandidateCard candidate={cand} />);
    const el = await screen.findByText(/剩 1 天/);
    expect(el.className).toContain("text-amber-600");
  });

  it("已过期且续期满(≥3次)无回调 → 纯灰字「已过期」", async () => {
    mockPrice["601012.SH"] = 23.0;
    const cand: Candidate = {
      ...base,
      expires_at: isoDaysFromNow(-3),
      renew_count: 3, // 续期满, canRenew=false, 且无 onReanalyze/onArchive → showMenu=false
    };
    withClient(<CompactCandidateCard candidate={cand} />);
    await screen.findByText(/距触发/);
    const el = screen.getByText("已过期");
    expect(el.className).toContain("text-flat");
    expect(el.tagName).not.toBe("BUTTON"); // 不是按钮, 是纯文字
  });

  it("已过期且可续 → 「已过期」是可点按钮, 点击展开三选一菜单", async () => {
    mockPrice["601012.SH"] = 23.0;
    const onReanalyze = vi.fn();
    const onArchive = vi.fn();
    const cand: Candidate = { ...base, expires_at: isoDaysFromNow(-3) };
    withClient(
      <CompactCandidateCard
        candidate={cand}
        onReanalyze={onReanalyze}
        onArchive={onArchive}
      />,
    );
    const btn = await screen.findByText(/已过期/);
    expect(btn.closest("button")).toBeTruthy();
    // 初始菜单未展开: "重分析"只在菜单里, 未展开应不存在
    expect(screen.queryByText("重分析")).toBeNull();
    // 点击展开
    fireEvent.click(btn);
    expect(await screen.findByText("重分析")).toBeTruthy();
    expect(screen.getByText(/续期/)).toBeTruthy();
    // 点重分析 → 回调 + 关菜单(重分析消失)
    fireEvent.click(screen.getByText("重分析"));
    expect(onReanalyze).toHaveBeenCalledWith(cand);
    expect(screen.queryByText("重分析")).toBeNull();
  });

  it("已过期续期满(≥3次)有回调 → 显示「已续3次, 该重分析」提示, 不给续期按钮", async () => {
    mockPrice["601012.SH"] = 23.0;
    const cand: Candidate = { ...base, expires_at: isoDaysFromNow(-3), renew_count: 3 };
    withClient(<CompactCandidateCard candidate={cand} onArchive={vi.fn()} />);
    fireEvent.click(await screen.findByText(/已过期/));
    expect(screen.getByText(/已续 3 次/)).toBeTruthy();
    expect(screen.queryByText(/续期$/)).toBeNull();
  });

  it("无 meta(历史候选) → 仍显示剩余天数, 不显示归因 tag", async () => {
    mockPrice["601012.SH"] = 23.0;
    const cand: Candidate = { ...base, expires_at: isoDaysFromNow(8) };
    withClient(<CompactCandidateCard candidate={cand} />);
    await screen.findByText(/距触发/);
    expect(screen.getByText(/剩 8 天/)).toBeTruthy();
    expect(screen.queryByText("·波动率算")).toBeNull();
    expect(screen.queryByText("·手动")).toBeNull();
  });
});
