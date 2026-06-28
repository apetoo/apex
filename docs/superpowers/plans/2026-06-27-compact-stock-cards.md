# 紧凑个股卡片 + 持仓/候选布局重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把行式个股卡片改成竖向紧凑网格卡,修正价格信息层次(当前价黑色、涨跌才上色),持仓页持仓/候选左右并列,概览页持仓满宽 3 列。

**Architecture:** 纯前端。新增 `CompactPositionCard`/`CompactCandidateCard` 两个竖向卡组件,改造 `PriceTag` 的价格染色(事实中性、评价上色),概览页和持仓页改用网格 + 新卡,删除旧行式 `PositionCard`/`CandidateCard`。不动后端,不加新数据字段。

**Tech Stack:** React 19 + TypeScript + Tailwind v4(@theme tokens:`text-up`红 `#e14b4b` / `text-down`绿 `#2ba84a` / `text-flat`灰 / `text-text-primary`黑 `#1a1a1a`) + TanStack Query v5 + Vitest + @testing-library/react。A股红涨绿跌铁律。

## Global Constraints

- 纯前端改动,不动 `backend/` / `apex/`,不加新数据字段。
- A股红涨绿跌:涨=红(`text-up`),跌=绿(`text-down`)。
- **信息层次铁律(本次核心):当前价 = 事实 → `text-text-primary` 黑色加粗,不随涨跌染色;涨跌额/幅 = 评价 → `directionClass` 红/绿。**
- 价格/涨跌额/百分比一律用 `.num` class(等宽 tabular-nums)。
- ts_code 一律带后缀(`600519.SH`)。
- 验证命令:`cd frontend && npx tsc --noEmit`(类型)、`cd frontend && npm test`(单测)、`cd frontend && npm run dev`(手动)。
- 单测不引入 MSW;react-query 组件用 `QueryClientProvider` wrapper + `vi.mock("@/api/market")`(参考 `src/api/__tests__/mutations.test.tsx` 的 wrapper 模式)。

## File Structure

- `frontend/src/components/a-share/PriceTag.tsx` — 改:价格 span 去掉 `directionClass`,改 `text-text-primary`。
- `frontend/src/components/a-share/__tests__/PriceTag.test.tsx` — 改:更新 3 个断言(涨/跌/平的价格元素不再含 up/down/flat)。
- `frontend/src/components/a-share/CompactPositionCard.tsx` — 新建:竖向紧凑持仓卡。
- `frontend/src/components/a-share/__tests__/CompactPositionCard.test.tsx` — 新建。
- `frontend/src/components/a-share/CompactCandidateCard.tsx` — 新建:竖向紧凑候选卡。
- `frontend/src/components/a-share/__tests__/CompactCandidateCard.test.tsx` — 新建。
- `frontend/src/routes/overview/OverviewPage.tsx` — 改:持仓列表 → `grid-cols-1 sm:grid-cols-2 lg:grid-cols-3` 网格 + `CompactPositionCard`(只读)。
- `frontend/src/routes/watchlist/WatchlistPage.tsx` — 改:持仓/候选 Card 上下堆叠 → `lg:grid-cols-2` 左右并列;内部各自 `xl:grid-cols-2`。
- `frontend/src/components/a-share/index.ts` — 改:移除 `PositionCard`/`CandidateCard` 导出,加 `CompactPositionCard`/`CompactCandidateCard`。
- `frontend/src/components/a-share/PositionCard.tsx` — 删除。
- `frontend/src/components/a-share/CandidateCard.tsx` — 删除。

---

### Task 1: PriceTag 价格信息层次修正(当前价黑色,涨跌才上色)

**Files:**
- Modify: `frontend/src/components/a-share/PriceTag.tsx`(末尾 return 的价格 span,约 line 95-100)
- Test: `frontend/src/components/a-share/__tests__/PriceTag.test.tsx`

**Interfaces:**
- Consumes: 无(纯展示组件)。
- Produces: `PriceTag` 同名同 props,行为变更 —— 价格元素不再含 `text-up`/`text-down`/`text-flat`,改为 `text-text-primary`。涨跌额/幅行不变(仍 `directionClass`)。后续 Task 2/3 的新卡依赖此行为。

- [ ] **Step 1: 更新失败测试(改 3 个断言)**

把 `frontend/src/components/a-share/__tests__/PriceTag.test.tsx` 里三个用例对**价格元素**的 className 断言,从"含 up/down/flat"改成"含 text-text-primary、不含 up/down/flat"。涨跌额/幅断言保留不动。

把第一个用例改成:

```tsx
  it("涨: price > prevClose → 当前价黑色(text-text-primary) + 正涨跌额/幅红字", () => {
    render(<PriceTag price={10.5} prevClose={10.0} />);
    const priceEl = screen.getByText("10.50");
    // 当前价 = 事实, 中性黑色, 不染红
    expect(priceEl.className).toContain("text-text-primary");
    expect(priceEl.className).not.toContain("text-up");
    // 涨跌额/幅 = 评价, 仍红字
    expect(screen.getByText(/\+0\.50/)).toBeTruthy();
    expect(screen.getByText(/\+5\.00%/)).toBeTruthy();
  });
```

把第二个用例(跌)改成:

```tsx
  it("跌: price < prevClose → 当前价黑色 + 负涨跌额/幅绿字", () => {
    render(<PriceTag price={9.5} prevClose={10.0} />);
    const priceEl = screen.getByText("9.50");
    expect(priceEl.className).toContain("text-text-primary");
    expect(priceEl.className).not.toContain("text-down");
    expect(screen.getByText(/-0\.50/)).toBeTruthy();
    expect(screen.getByText(/-5\.00%/)).toBeTruthy();
  });
```

把第三个用例(平)改成 —— 注意 `price === prevClose` 时 `delta=0`,价格仍应黑色(不再 flat):

```tsx
  it("平: price == prevClose → 当前价黑色(非 flat)", () => {
    render(<PriceTag price={10.0} prevClose={10.0} />);
    const priceEl = screen.getByText("10.00");
    expect(priceEl.className).toContain("text-text-primary");
    expect(priceEl.className).not.toContain("text-flat");
  });
```

其余用例(盘外/停牌/loading/error/showChange=false)不动。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/PriceTag.test.tsx`
Expected: FAIL —— 3 个用例因价格元素仍带 `text-up`/`text-down`/`text-flat` 而断言 `not.toContain` 失败。

- [ ] **Step 3: 改 PriceTag 价格 span**

`frontend/src/components/a-share/PriceTag.tsx` 末尾 return 中,价格那个 span:

旧:
```tsx
        <span className={cn("num font-medium", sizeCls, directionClass(delta))}>
          {formatPrice(price)}
        </span>
```

新(去掉 `directionClass(delta)`,加 `text-text-primary`):
```tsx
        <span className={cn("num font-medium", sizeCls, "text-text-primary")}>
          {formatPrice(price)}
        </span>
```

下方的涨跌额/幅 `<span className={cn("num text-xs", directionClass(delta))}>` **不动**。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/PriceTag.test.tsx`
Expected: PASS(全部用例)。

- [ ] **Step 5: 全量类型 + 单测回归**

Run: `cd frontend && npx tsc --noEmit && npm test`
Expected: tsc 通过;既有 62 单测全过(PriceTag 3 个改了断言,其余不回归)。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/a-share/PriceTag.tsx frontend/src/components/a-share/__tests__/PriceTag.test.tsx
git commit -m "feat(frontend): PriceTag 当前价改黑色,涨跌色仅留给涨跌额/幅

事实(当前价)中性黑、评价(涨跌)上色的信息层次修正。"
```

---

### Task 2: 新建 CompactPositionCard(竖向紧凑持仓卡)

**Files:**
- Create: `frontend/src/components/a-share/CompactPositionCard.tsx`
- Test: `frontend/src/components/a-share/__tests__/CompactPositionCard.test.tsx`

**Interfaces:**
- Consumes: `ActivePosition`(from `@/api/watchlist`);`getPrices`/`getDailyPrices`(from `@/api/market`);`qk.prices`/`qk.dailyPrices`(from `@/api/query-keys`);`PriceTag`(Task 1 改造后,价格黑色);`cn`/`formatPrice`(from `@/lib/utils`)。
- Produces: `CompactPositionCard({ position, onAdd?, onReduce? })` —— Props 与旧 `PositionCard` 一致。`onAdd`/`onReduce` 任一存在才渲染按钮行;都不传则只读(概览页用)。Task 4/5 依赖此组件。

- [ ] **Step 1: 写失败测试**

Create `frontend/src/components/a-share/__tests__/CompactPositionCard.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { CompactPositionCard } from "../CompactPositionCard";
import type { ActivePosition } from "@/api/watchlist";

// mock 行情: 600519.SH 当前 100.0, 昨收 98.0
vi.mock("@/api/market", () => ({
  getPrices: vi.fn(async (codes: string[]) =>
    Object.fromEntries(codes.map((c) => [c, 100.0]))),
  getDailyPrices: vi.fn(async (codes: string[]) =>
    Object.fromEntries(codes.map((c) => [c, 98.0]))),
}));

function withClient(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const pos = {
  ts_code: "600519.SH",
  name: "贵州茅台",
  entry_price: 90,
  avg_cost: 90,
  stop_loss: 85,
  target: 110,
  position_size_shares: 100,
  strategy: "价值",
} as unknown as ActivePosition;

describe("CompactPositionCard", () => {
  it("渲染名称/代码/策略, 当前价黑色(非红绿)", async () => {
    withClient(<CompactPositionCard position={pos} />);
    expect(screen.getByText("贵州茅台")).toBeTruthy();
    expect(screen.getByText("价值")).toBeTruthy();
    expect(screen.getByText("600519.SH")).toBeTruthy();
    const priceEl = await screen.findByText("100.00");
    expect(priceEl.className).toContain("text-text-primary");
    expect(priceEl.className).not.toContain("text-up");
    expect(priceEl.className).not.toContain("text-down");
  });

  it("只读(未传 onAdd/onReduce) → 不渲染加仓/减仓按钮", async () => {
    withClient(<CompactPositionCard position={pos} />);
    await screen.findByText("100.00");
    expect(screen.queryByText("加仓")).toBeNull();
    expect(screen.queryByText("减仓")).toBeNull();
  });

  it("传 onAdd/onReduce → 渲染按钮且可点", async () => {
    const onAdd = vi.fn();
    const onReduce = vi.fn();
    withClient(
      <CompactPositionCard position={pos} onAdd={onAdd} onReduce={onReduce} />,
    );
    const addBtn = await screen.findByText("加仓");
    expect(screen.getByText("减仓")).toBeTruthy();
    addBtn.click();
    expect(onAdd).toHaveBeenCalledWith(pos);
  });

  it("成本/止损/目标参数行渲染", async () => {
    withClient(<CompactPositionCard position={pos} />);
    await screen.findByText("100.00");
    // 100 股 @ 90.00
    expect(screen.getByText(/100\s*股/)).toBeTruthy();
    expect(screen.getByText(/90\.00/)).toBeTruthy();
    // 止损 85.00 / 目标 110.00
    expect(screen.getByText("85.00")).toBeTruthy();
    expect(screen.getByText("110.00")).toBeTruthy();
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/CompactPositionCard.test.tsx`
Expected: FAIL —— `CompactPositionCard` 未定义/未导出,模块找不到。

- [ ] **Step 3: 实现 CompactPositionCard**

Create `frontend/src/components/a-share/CompactPositionCard.tsx`:

```tsx
import { ArrowUpRight, ArrowDownRight, Target, ShieldAlert } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { PriceTag } from "@/components/a-share";
import { getPrices, getDailyPrices } from "@/api/market";
import { qk } from "@/api/query-keys";
import { cn, formatPrice } from "@/lib/utils";
import type { ActivePosition } from "@/api/watchlist";

/**
 * <CompactPositionCard> — 竖向紧凑持仓卡(网格用)
 *
 * 替代旧行式 PositionCard。一行 2-3 张排列。
 * 信息层次: 当前价(黑色加粗, PriceTag) → 涨跌(红/绿) → 持仓盈亏(红/绿)
 *          → 参数行(股数@成本 / 止损/目标 灰小字) → [加仓][减仓](可选)
 *
 * onAdd/onReduce 任一存在才渲染按钮行;都不传 = 只读(概览页用)。
 * 红涨绿跌铁律: 盈亏 = (当前价 - avg_cost) * 股数。
 */
export function CompactPositionCard({
  position,
  onAdd,
  onReduce,
}: {
  position: ActivePosition;
  onAdd?: (p: ActivePosition) => void;
  onReduce?: (p: ActivePosition) => void;
}) {
  const { ts_code, name, entry_price, avg_cost, stop_loss, target, position_size_shares, strategy } =
    position;

  const prices = useQuery({
    queryKey: qk.prices([ts_code]),
    queryFn: () => getPrices([ts_code]),
  });
  const daily = useQuery({
    queryKey: qk.dailyPrices([ts_code]),
    queryFn: () => getDailyPrices([ts_code]),
  });

  const currentPrice = prices.data?.[ts_code] ?? null;
  const prevClose = daily.data?.[ts_code] ?? null;
  const loading = prices.isLoading || daily.isLoading;
  const error = prices.isError || daily.isError;

  const cost = avg_cost ?? entry_price;
  const pnl =
    currentPrice != null && position_size_shares != null
      ? (currentPrice - cost) * position_size_shares
      : null;
  const pnlDelta = currentPrice != null ? currentPrice - cost : null;

  return (
    <div className="rounded-lg border border-border bg-bg-card p-3">
      {/* 名称 + 策略 tag */}
      <div className="flex items-center gap-2">
        <p className="truncate font-medium">{name}</p>
        {strategy && (
          <span className="rounded bg-bg-base px-1.5 py-0.5 text-[10px] text-text-secondary">
            {strategy}
          </span>
        )}
      </div>
      <p className="mt-0.5 text-xs text-text-secondary">{ts_code}</p>

      {/* 当前价(黑色) + 涨跌(红/绿) */}
      <div className="mt-2">
        <PriceTag
          price={currentPrice}
          prevClose={prevClose}
          loading={loading}
          error={error}
          size="lg"
          showChange
        />
      </div>

      {/* 持仓盈亏 */}
      {pnl != null && (
        <div
          className={cn(
            "num mt-1 flex items-center gap-0.5 text-xs",
            pnl >= 0 ? "text-up" : "text-down",
          )}
        >
          {pnl >= 0 ? (
            <ArrowUpRight className="h-3 w-3" />
          ) : (
            <ArrowDownRight className="h-3 w-3" />
          )}
          <span>
            {pnl >= 0 ? "+" : ""}
            {pnl.toFixed(0)} 元
          </span>
          <span className="ml-1 opacity-80">
            ({pnlDelta != null ? ((pnlDelta / cost) * 100).toFixed(2) : "—"}%)
          </span>
        </div>
      )}

      <div className="my-2 border-t border-border" />

      {/* 参数行: 股数@成本 / 止损 / 目标 */}
      <div className="space-y-1 text-[11px] text-text-secondary">
        <div className="num">
          {position_size_shares != null && <span>{position_size_shares} 股</span>}
          {avg_cost != null && <span> @ {formatPrice(avg_cost)}</span>}
          {position_size_shares == null && avg_cost == null && <span>—</span>}
        </div>
        {(stop_loss != null || target != null) && (
          <div className="num flex items-center gap-3">
            {stop_loss != null && (
              <span className="flex items-center gap-0.5">
                <ShieldAlert className="h-3 w-3 text-down" />
                {formatPrice(stop_loss)}
              </span>
            )}
            {target != null && (
              <span className="flex items-center gap-0.5">
                <Target className="h-3 w-3 text-up" />
                {formatPrice(target)}
              </span>
            )}
          </div>
        )}
      </div>

      {/* 操作按钮(可选) */}
      {(onAdd || onReduce) && (
        <div className="mt-2 flex items-center gap-1.5">
          {onAdd && (
            <button
              type="button"
              onClick={() => onAdd(position)}
              className="rounded border border-border px-2 py-0.5 text-[11px] text-text-secondary hover:bg-bg-base"
            >
              加仓
            </button>
          )}
          {onReduce && (
            <button
              type="button"
              onClick={() => onReduce(position)}
              className="rounded border border-border px-2 py-0.5 text-[11px] text-text-secondary hover:bg-bg-base"
            >
              减仓
            </button>
          )}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/CompactPositionCard.test.tsx`
Expected: PASS(4 个用例)。

- [ ] **Step 5: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 通过(此时组件未被引用也无妨,TS 不报未使用导出)。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/a-share/CompactPositionCard.tsx frontend/src/components/a-share/__tests__/CompactPositionCard.test.tsx
git commit -m "feat(frontend): 新增 CompactPositionCard 竖向紧凑持仓卡"
```

---

### Task 3: 新建 CompactCandidateCard(竖向紧凑候选卡)

**Files:**
- Create: `frontend/src/components/a-share/CompactCandidateCard.tsx`
- Test: `frontend/src/components/a-share/__tests__/CompactCandidateCard.test.tsx`

**Interfaces:**
- Consumes: `Candidate`(from `@/api/watchlist`);`getPrices`(from `@/api/market`);`qk.prices`;`PriceTag`(Task 1);`cn`/`formatPrice`。
- Produces: `CompactCandidateCard({ candidate })` —— Props 与旧 `CandidateCard` 一致。无加仓/减仓按钮。Task 5 依赖此组件。

**配色规则(本次锁定):**
- 未触发 → 距触发行 `text-text-secondary`(灰)。
- 接近触发(未触发且 `|distancePct| < 2`)→ `text-amber-600`(黄,Tailwind v4 默认调色板自带)。
- 已触发 → 卡片底 `bg-up/5`(浅红,A股红涨=利好触发)+ 名称旁 `已触发` 红色 tag + 距触发行替换为 `已触发` 红字(`text-up`)。

- [ ] **Step 1: 写失败测试**

Create `frontend/src/components/a-share/__tests__/CompactCandidateCard.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { CompactCandidateCard } from "../CompactCandidateCard";
import type { Candidate } from "@/api/watchlist";

// mock 行情: 当前价可逐用例覆盖
const mockPrice: Record<string, number> = {};
vi.mock("@/api/market", () => ({
  getPrices: vi.fn(async (codes: string[]) =>
    Object.fromEntries(codes.map((c) => [c, mockPrice[c] ?? 22.15]))),
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
    mockPrice["601012.SH"] = 22.2; // (22.2-22.5)/22.5 = -1.33% → 接近
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
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/CompactCandidateCard.test.tsx`
Expected: FAIL —— `CompactCandidateCard` 未定义。

- [ ] **Step 3: 实现 CompactCandidateCard**

Create `frontend/src/components/a-share/CompactCandidateCard.tsx`:

```tsx
import { Bell, TrendingDown, TrendingUp, Target, ShieldAlert } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { PriceTag } from "@/components/a-share";
import { getPrices } from "@/api/market";
import { qk } from "@/api/query-keys";
import { cn, formatPrice } from "@/lib/utils";
import type { Candidate } from "@/api/watchlist";

/**
 * <CompactCandidateCard> — 竖向紧凑候选卡(网格用)
 *
 * 替代旧行式 CandidateCard。无加仓/减仓按钮(候选未持仓)。
 * 距触发距离配色: 未触发灰 / 接近触发(|pct|<2)黄 / 已触发红字+浅红底。
 * 红涨绿跌铁律: 触发=利好 → 用 up(红) 表达已触发。
 */
export function CompactCandidateCard({ candidate }: { candidate: Candidate }) {
  const { ts_code, name, trigger_price, trigger_direction, stop_advice, target_advice } =
    candidate;

  const prices = useQuery({
    queryKey: qk.prices([ts_code]),
    queryFn: () => getPrices([ts_code]),
  });

  const currentPrice = prices.data?.[ts_code] ?? null;
  const loading = prices.isLoading;
  const error = prices.isError;

  const distance =
    currentPrice != null ? currentPrice - trigger_price : null;
  const distancePct =
    currentPrice != null && trigger_price !== 0
      ? (distance! / trigger_price) * 100
      : null;

  const isTriggered =
    currentPrice != null &&
    (trigger_direction === "below"
      ? currentPrice <= trigger_price
      : currentPrice >= trigger_price);
  const isNear =
    !isTriggered && distancePct != null && Math.abs(distancePct) < 2;

  const TrendingIcon = trigger_direction === "below" ? TrendingDown : TrendingUp;

  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-bg-card p-3",
        isTriggered && "bg-up/5",
      )}
    >
      {/* 名称 + 已触发 tag */}
      <div className="flex items-center gap-2">
        <p className="truncate font-medium">{name}</p>
        {isTriggered && (
          <span className="inline-flex items-center gap-0.5 rounded bg-up/10 px-1.5 py-0.5 text-[10px] font-medium text-up">
            <Bell className="h-2.5 w-2.5" /> 已触发
          </span>
        )}
      </div>
      <p className="mt-0.5 text-xs text-text-secondary">{ts_code}</p>

      {/* 当前价(黑色, 候选不展示当日涨跌) */}
      <div className="mt-2">
        <PriceTag
          price={currentPrice}
          prevClose={null}
          loading={loading}
          error={error}
          size="lg"
          showChange={false}
        />
      </div>

      {/* 距触发 / 已触发 */}
      {isTriggered ? (
        <p className="num mt-1 text-xs text-up">已触发</p>
      ) : distance != null && distancePct != null ? (
        <p
          className={cn(
            "num mt-1 text-xs",
            isNear ? "text-amber-600" : "text-text-secondary",
          )}
        >
          距触发 {distance >= 0 ? "+" : ""}
          {distance.toFixed(2)} ({distancePct >= 0 ? "+" : ""}
          {distancePct.toFixed(2)}%)
        </p>
      ) : null}

      <div className="my-2 border-t border-border" />

      {/* 参数行: 触发价+方向 / 建议止损 / 目标 */}
      <div className="space-y-1 text-[11px] text-text-secondary">
        <div className="num flex items-center gap-0.5">
          <TrendingIcon className="h-3 w-3" />
          触发 {formatPrice(trigger_price)}(
          {trigger_direction === "below" ? "下方" : "上方"})
        </div>
        {(stop_advice > 0 || target_advice > 0) && (
          <div className="num flex items-center gap-3">
            {stop_advice > 0 && (
              <span className="flex items-center gap-0.5">
                <ShieldAlert className="h-3 w-3 text-down" />
                {formatPrice(stop_advice)}
              </span>
            )}
            {target_advice > 0 && (
              <span className="flex items-center gap-0.5">
                <Target className="h-3 w-3 text-up" />
                {formatPrice(target_advice)}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/CompactCandidateCard.test.tsx`
Expected: PASS(4 个用例)。

- [ ] **Step 5: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 通过。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/a-share/CompactCandidateCard.tsx frontend/src/components/a-share/__tests__/CompactCandidateCard.test.tsx
git commit -m "feat(frontend): 新增 CompactCandidateCard 竖向紧凑候选卡"
```

---

### Task 4: 概览页持仓列表网格化(满宽 3 列,只读)

**Files:**
- Modify: `frontend/src/routes/overview/OverviewPage.tsx`(持仓列表渲染块,约 line 285-360)

**Interfaces:**
- Consumes: `CompactPositionCard`(Task 2);`positions`/`prices`/`daily` 已有 query(本文件已存在,不动)。
- Produces: 概览页持仓区改为网格。

注意:`pnlMap`/`todayPnlMap` 仍用于顶部「今日盈亏汇总」Card,**保留不动**;只是不再用于单卡渲染(卡内自己算)。

- [ ] **Step 1: 替换持仓列表渲染块**

`OverviewPage.tsx` 中,持仓 Card 的 `<CardContent className="pt-0">` 内,把原 `positions.map((pos) => { ... return <div ...>行式...</div> })` 整段替换为:

```tsx
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {positions.map((pos) => (
                <CompactPositionCard key={pos.ts_code} position={pos} />
              ))}
            </div>
          )}
```

(原 `{positions.length === 0 ? (...) : (` 的空态分支保留,只替换 else 分支。)

- [ ] **Step 2: 调整 import**

`OverviewPage.tsx` 顶部 import 区:
- 删除已不再使用的图标/组件引用 —— 替换后该渲染块不再直接用 `ShieldAlert`/`Target`/`PriceTag`/`ArrowUpRight`/`ArrowDownRight`(它们随旧行式 JSX 一起删掉)。逐个检查这些符号是否在文件其他位置仍被使用,只删确实不再使用的 import。
- 新增:`import { CompactPositionCard } from "@/components/a-share";`

实现者注意:用 `npx tsc --noEmit` 驱动 —— 删多了会报"未定义",删少了会报"未使用"(若 noUnusedLocals 开启)。以 tsc 通过为准。

- [ ] **Step 3: 类型检查 + 全量单测**

Run: `cd frontend && npx tsc --noEmit && npm test`
Expected: tsc 通过;单测全过。

- [ ] **Step 4: 手动验证**

Run: `cd frontend && npm run dev`(需后端在 8000,或用 mock)
验证:
- 概览页持仓区一行最多 3 张卡(宽屏),窄屏回退 2 列 / 1 列。
- 持仓卡当前价黑色加粗,涨跌红/绿;**无**加仓/减仓按钮(只读)。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/overview/OverviewPage.tsx
git commit -m "feat(frontend): 概览页持仓列表改网格(满宽3列,只读)"
```

---

### Task 5: 持仓页持仓/候选左右并列 + 内部网格

**Files:**
- Modify: `frontend/src/routes/watchlist/WatchlistPage.tsx`(持仓 Card + 候选 Card 区块)

**Interfaces:**
- Consumes: `CompactPositionCard`(Task 2)+ `onAdd`/`onReduce`(接现有 `openTradeForm`);`CompactCandidateCard`(Task 3)。
- Produces: 持仓/候选左右并列布局。交易流水 Card、平仓复盘 Card 保持满宽不动。

- [ ] **Step 1: 改持仓/候选为左右并列**

`WatchlistPage.tsx` 中,`data ? (...)` 分支内现在有「持仓 section Card」和「候选 section Card」上下两段。用一个 `grid` 包起来左右并列。把这两段替换为:

```tsx
        <>
          <div className="grid gap-4 lg:grid-cols-2">
            {/* 持仓 section */}
            <Card>
              <CardHeader className="flex flex-row items-center justify-between space-y-0">
                <div className="flex items-center gap-2">
                  <Wallet className="h-4 w-4 text-text-secondary" />
                  <CardTitle>持仓</CardTitle>
                  <span className="num text-xs text-text-secondary">
                    {data.active_positions.length} 只
                  </span>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => navigate("/analyze")}
                >
                  去分析 →
                </Button>
              </CardHeader>
              {data.active_positions.length === 0 ? (
                <CardContent>
                  <p className="py-6 text-center text-sm text-flat">暂无持仓</p>
                </CardContent>
              ) : (
                <CardContent className="pt-0">
                  <div className="grid grid-cols-1 gap-2 xl:grid-cols-2">
                    {data.active_positions.map((p) => (
                      <CompactPositionCard
                        key={p.ts_code}
                        position={p}
                        onAdd={(pos) => openTradeForm(pos, "buy")}
                        onReduce={(pos) => openTradeForm(pos, "sell")}
                      />
                    ))}
                  </div>
                </CardContent>
              )}
            </Card>

            {/* 候选 section */}
            <Card>
              <CardHeader className="flex flex-row items-center justify-between space-y-0">
                <div className="flex items-center gap-2">
                  <Bell className="h-4 w-4 text-text-secondary" />
                  <CardTitle>候选</CardTitle>
                  <span className="num text-xs text-text-secondary">
                    {data.candidates.length} 只
                  </span>
                </div>
                <CardDescription>触发价 + 方向, 接近时高亮</CardDescription>
              </CardHeader>
              {data.candidates.length === 0 ? (
                <CardContent>
                  <p className="py-6 text-center text-sm text-flat">暂无候选</p>
                </CardContent>
              ) : (
                <CardContent className="pt-0">
                  <div className="grid grid-cols-1 gap-2 xl:grid-cols-2">
                    {data.candidates.map((c) => (
                      <CompactCandidateCard key={c.ts_code} candidate={c} />
                    ))}
                  </div>
                </CardContent>
              )}
            </Card>
          </div>
        </>
```

(交易流水 Card、平仓复盘 Card 在此 `</>` 之后,保持原样满宽。)

- [ ] **Step 2: 调整 import**

`WatchlistPage.tsx` 顶部:
- 旧:`import { PositionCard, CandidateCard } from "@/components/a-share";`
- 新:`import { CompactPositionCard, CompactCandidateCard } from "@/components/a-share";`

- [ ] **Step 3: 类型检查 + 全量单测**

Run: `cd frontend && npx tsc --noEmit && npm test`
Expected: tsc 通过;单测全过。

- [ ] **Step 4: 手动验证**

Run: `cd frontend && npm run dev`
验证:
- 宽屏:持仓在左、候选在右并列;每边内部 1-2 张卡。
- 窄屏(<lg):自动回退上下堆叠。
- 持仓卡有加仓/减仓按钮,点开现有加仓/减仓表单(`openTradeForm`)正常。
- 候选卡:未触发灰、接近黄、已触发红字+浅红底。
- 交易流水 Card 仍满宽在下方。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/watchlist/WatchlistPage.tsx
git commit -m "feat(frontend): 持仓页持仓/候选左右并列 + 内部网格"
```

---

### Task 6: 删除旧 PositionCard/CandidateCard + 更新 index 导出

**Files:**
- Delete: `frontend/src/components/a-share/PositionCard.tsx`
- Delete: `frontend/src/components/a-share/CandidateCard.tsx`
- Modify: `frontend/src/components/a-share/index.ts`

**Interfaces:**
- Consumes: Task 4/5 已把所有引用切到 `Compact*` 版本。
- Produces: 目录干净,无两套卡片组件并存。

- [ ] **Step 1: 确认无残留引用**

Run: `cd frontend && grep -rn "PositionCard\|CandidateCard" src --include="*.ts" --include="*.tsx" | grep -v "Compact" | grep -v "__tests__/PositionCard\|__tests__/CandidateCard"`
Expected: 仅命中 `index.ts` 的导出行 + 两个待删文件自身(若有旧测试文件也一并列出)。`OverviewPage`/`WatchlistPage` 不应再出现裸 `PositionCard`/`CandidateCard`。

- [ ] **Step 2: 更新 index 导出**

`frontend/src/components/a-share/index.ts` 改为(移除旧两个,加新两个):

```ts
export { PriceTag } from "./PriceTag";
export { VerdictTag } from "./VerdictTag";
export { MarketIndexBar } from "./MarketIndexBar";
export { CompactPositionCard } from "./CompactPositionCard";
export { CompactCandidateCard } from "./CompactCandidateCard";
export { AnalyzeTraceStream } from "./AnalyzeTraceStream";
export { VerdictDetailCard, fmtPrice } from "./VerdictDetailCard";
export { IntradayChart } from "./IntradayChart";
```

- [ ] **Step 3: 删除旧文件**

```bash
git rm frontend/src/components/a-share/PositionCard.tsx
git rm frontend/src/components/a-share/CandidateCard.tsx
```

(若存在旧的 `__tests__/PositionCard.test.tsx`/`CandidateCard.test.tsx` 一并 `git rm` —— Step 1 的 grep 会暴露。)

- [ ] **Step 4: 类型检查 + 全量单测**

Run: `cd frontend && npx tsc --noEmit && npm test`
Expected: tsc 通过(无悬空 import);单测全过,旧组件测试已删,不回归。

- [ ] **Step 5: Commit**

```bash
git add -A frontend/src/components/a-share/
git commit -m "chore(frontend): 删除旧行式 PositionCard/CandidateCard,统一用 Compact 版"
```

---

## 完成判据

- [ ] `cd frontend && npx tsc --noEmit` 通过。
- [ ] `cd frontend && npm test` 全过(新 8 个用例 + 既有不回归)。
- [ ] 概览页:持仓满宽 3 列网格,卡内当前价黑色、无加仓/减仓按钮。
- [ ] 持仓页:持仓/候选左右并列(宽屏),持仓卡有加仓/减仓,候选卡三态配色正确。
- [ ] `PriceTag` 在所有调用处当前价均黑色。
- [ ] 旧 `PositionCard`/`CandidateCard` 已删除,无悬空引用。
