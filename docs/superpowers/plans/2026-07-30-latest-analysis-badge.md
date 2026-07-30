# 最近分析徽标 + hover 浮窗 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 持仓卡/候选卡上加一行常驻「最近分析」徽标,hover 弹浮窗看核心判断,点击开 Drawer 看完整分析。

**Architecture:** 新组件 `LatestAnalysisBadge`(徽标行 + CSS group-hover 浮窗 + Drawer 三合一),数据走已有 `GET /api/journal/{ts_code}/latest`(`getLatestJournal` + `qk.journalLatest`),Drawer 内复用 `VerdictDetailCard`。零后端改动。Spec: `docs/superpowers/specs/2026-07-30-latest-analysis-badge-design.md`。

**Tech Stack:** React 19 + TypeScript + Tailwind v4 + TanStack Query v5 + Vitest + testing-library。

## Global Constraints

- 红涨绿跌铁律:看多/加仓/收紧止损 → `text-up`(红);看空/减仓/清仓/放宽止损 → `text-down`(绿);中性/持有 → `text-flat`/`text-text-primary`。
- 测试命令:`cd frontend && npx vitest run <file>`;类型检查 `cd frontend && npx tsc --noEmit`。
- commit message 用中文 conventional commit,结尾带 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- 陈旧阈值:`> 5 天` 灰化 + 显示 `N 天前`。
- 浮窗证据链只取前 2 条,rationale clamp 4 行。

---

### Task 1: LatestAnalysisBadge 组件 + 类型扩展 + 单测

**Files:**
- Modify: `frontend/src/api/analyze.ts`(拓宽 `LatestJournal` 类型,纯类型无运行时变化)
- Create: `frontend/src/components/a-share/LatestAnalysisBadge.tsx`
- Modify: `frontend/src/components/a-share/index.ts`(加导出)
- Test: `frontend/src/components/a-share/__tests__/LatestAnalysisBadge.test.tsx`

**Interfaces:**
- Consumes: `getLatestJournal(tsCode)` / `qk.journalLatest(tsCode)`(均已存在);`VerdictDetailCard`、`VerdictTag`、`Drawer`(均已存在);`verdictColor`(`@/types/verdict`);`cn`/`formatPrice`(`@/lib/utils`)。
- Produces: `<LatestAnalysisBadge tsCode: string stopBefore?: number | null />` —— Task 2/3 的两张卡片以此为唯一接入点。`stopBefore` 仅持仓卡传(position_action 止损变动方向箭头用)。

- [ ] **Step 1: 拓宽 `LatestJournal` 类型**

`frontend/src/api/analyze.ts` 中把 `LatestJournal` 替换为:

```ts
export interface LatestJournal {
  ts_code: string;
  analyzed_at?: string;
  source?: string;
  verdict?: string;
  confidence?: number;
  calibrated_confidence?: number;
  evidence?: string[];
  price_advice?: {
    entry?: number | null;
    entry_low?: number | null;
    entry_high?: number | null;
    stop_loss?: number | null;
    target?: number | null;
  };
  position_action?: {
    action?: string;
    add_shares?: number | null;
    trim_shares?: number | null;
    trim_pct?: number | null;
    new_stop?: number | null;
    rationale?: string;
    scale_plan?: unknown[];
  };
}
```

- [ ] **Step 2: 写失败测试**

创建 `frontend/src/components/a-share/__tests__/LatestAnalysisBadge.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { LatestAnalysisBadge } from "../LatestAnalysisBadge";
import { getLatestJournal } from "@/api/analyze";

vi.mock("@/api/analyze", () => ({
  getLatestJournal: vi.fn(async () => null),
}));

const mocked = vi.mocked(getLatestJournal);

function withClient(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const verdictEntry = {
  ts_code: "600519.SH",
  analyzed_at: new Date(Date.now() - 86400000).toISOString(), // 1 天前, 不陈旧
  source: "verdict",
  verdict: "看多",
  confidence: 68,
  calibrated_confidence: 62,
  evidence: ["业绩预增40%,北向连续3日增持", "回踩20日线企稳,量能配合", "第三条不该显示"],
  price_advice: { entry: null, entry_low: 12.3, entry_high: 12.6, stop_loss: 11.8, target: 14.5 },
};

const paEntry = {
  ts_code: "600519.SH",
  analyzed_at: new Date(Date.now() - 86400000).toISOString(),
  source: "position_action",
  position_action: {
    action: "trim",
    trim_shares: 300,
    new_stop: 12.8,
    rationale: "放量滞涨,龙头地位被挑战,先锁定部分利润。",
    scale_plan: [{ level: 1 }, { level: 2 }],
  },
};

describe("LatestAnalysisBadge", () => {
  it("无 journal -> 不渲染任何内容", async () => {
    mocked.mockResolvedValueOnce(null);
    const { container } = withClient(<LatestAnalysisBadge tsCode="600519.SH" />);
    // 等 query settle
    await new Promise((r) => setTimeout(r, 0));
    expect(container.innerHTML).toBe("");
  });

  it("verdict entry -> 徽标: 看多 + 校准62 + 日期; 浮窗: 三价 + 前2条证据", async () => {
    mocked.mockResolvedValueOnce(verdictEntry as never);
    withClient(<LatestAnalysisBadge tsCode="600519.SH" />);
    expect(await screen.findByText("看多")).toBeTruthy();
    expect(screen.getByText(/校准\s*62/)).toBeTruthy();
    expect(screen.getByText(/^\d{2}-\d{2}$/)).toBeTruthy(); // 日期 MM-DD
    // 浮窗内容在 DOM 中(group-hover 只是 CSS 隐藏, jsdom 可直接断言)
    expect(screen.getByText(/入场 12.30–12.60/)).toBeTruthy();
    expect(screen.getByText(/止损 11.80/)).toBeTruthy();
    expect(screen.getByText(/目标 14.50/)).toBeTruthy();
    expect(screen.getByText(/业绩预增40%/)).toBeTruthy();
    expect(screen.getByText(/回踩20日线/)).toBeTruthy();
    expect(screen.queryByText(/第三条不该显示/)).toBeNull(); // 只取前 2 条
    expect(screen.getByText(/点击查看完整分析/)).toBeTruthy();
  });

  it("position_action entry -> 徽标: 减仓 + 止损; 浮窗: rationale + 触发计划条数", async () => {
    mocked.mockResolvedValueOnce(paEntry as never);
    withClient(<LatestAnalysisBadge tsCode="600519.SH" stopBefore={12.3} />);
    expect(await screen.findByText("减仓")).toBeTruthy();
    expect(screen.getAllByText(/12\.80/).length).toBeGreaterThanOrEqual(1);
    // 浮窗
    expect(screen.getByText(/-300股/)).toBeTruthy();
    expect(screen.getByText(/放量滞涨/)).toBeTruthy();
    expect(screen.getByText(/未来触发计划 2 条/)).toBeTruthy();
    // stopBefore=12.3 < 12.8 -> 收紧(红涨: 收紧=up)
    expect(screen.getAllByText("↑收紧").length).toBeGreaterThanOrEqual(1);
  });

  it("陈旧(>5 天) -> 显示 N 天前", async () => {
    mocked.mockResolvedValueOnce({
      ...verdictEntry,
      analyzed_at: new Date(Date.now() - 12 * 86400000).toISOString(),
    } as never);
    withClient(<LatestAnalysisBadge tsCode="600519.SH" />);
    expect(await screen.findByText(/天前/)).toBeTruthy();
  });

  it("点击徽标 -> Drawer 打开渲染 VerdictDetailCard", async () => {
    mocked.mockResolvedValueOnce(verdictEntry as never);
    withClient(<LatestAnalysisBadge tsCode="600519.SH" />);
    const badge = await screen.findByText("看多");
    fireEvent.click(badge.closest("button")!);
    // Drawer 标题 + VerdictDetailCard 的"分析结果"
    expect(await screen.findByText("最近分析")).toBeTruthy();
    expect(screen.getByText("分析结果")).toBeTruthy();
    expect(screen.getByText("证据链")).toBeTruthy();
  });
});
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/LatestAnalysisBadge.test.tsx`
Expected: FAIL(组件不存在,import 报错)

- [ ] **Step 4: 实现组件**

创建 `frontend/src/components/a-share/LatestAnalysisBadge.tsx`:

```tsx
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { Drawer } from "@/components/base";
import { VerdictDetailCard, VerdictTag } from "@/components/a-share";
import { getLatestJournal, type LatestJournal } from "@/api/analyze";
import { qk } from "@/api/query-keys";
import { cn, formatPrice } from "@/lib/utils";
import { verdictColor } from "@/types/verdict";

/**
 * <LatestAnalysisBadge> — 卡片上的「最近分析」徽标行 + hover 浮窗 + 点击 Drawer
 *
 * 数据: GET /api/journal/{ts_code}/latest(react-query 按 ts_code 缓存, staleTime 5min)。
 * 两种 kind 自动换措辞:
 *   - source === "position_action"(持仓常见): 动作 + 新止损(+方向箭头, 需 stopBefore)
 *   - 否则 verdict: 方向 + 校准置信度
 * 陈旧(>5 天)整行灰化 + 日期改显 "N 天前"。无 journal / loading -> 不渲染(零占位)。
 * hover 浮窗纯 CSS group-hover, 移动端无 hover 自然退化为点击 -> Drawer(复用 VerdictDetailCard)。
 */

/** 陈旧阈值(天)。 */
const STALE_DAYS = 5;

const ACTION_LABELS: Record<string, string> = {
  hold: "持有",
  add: "加仓",
  trim: "减仓",
  exit: "清仓",
};

function actionTone(action: string): string {
  if (action === "add") return "text-up";
  if (action === "trim" || action === "exit") return "text-down";
  return "text-text-primary";
}

/** 距今整天数; 解析失败返回 null。 */
function daysSince(iso?: string): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86400000);
}

export function LatestAnalysisBadge({
  tsCode,
  stopBefore,
}: {
  tsCode: string;
  /** 旧止损(持仓卡传 plan.last_stop_before ?? stop_loss), 用于 position_action 止损方向箭头。 */
  stopBefore?: number | null;
}) {
  const { data } = useQuery({
    queryKey: qk.journalLatest(tsCode),
    queryFn: () => getLatestJournal(tsCode),
    staleTime: 5 * 60_000,
  });
  const [drawerOpen, setDrawerOpen] = useState(false);

  if (!data) return null; // loading / 无记录 / 错误 -> 零占位

  const isPa = data.source === "position_action";
  const days = daysSince(data.analyzed_at);
  const stale = days != null && days > STALE_DAYS;
  const dateLabel =
    stale && days != null
      ? `${days} 天前`
      : (data.analyzed_at ?? "").slice(5, 10) || "-";

  return (
    <div className="group relative mt-1">
      {/* 徽标行(常驻, 点击开 Drawer) */}
      <button
        type="button"
        onClick={() => setDrawerOpen(true)}
        className={cn(
          "flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left text-[11px] hover:bg-bg-base",
          stale ? "text-flat" : "text-text-secondary",
        )}
        title="查看最近一次 AI 分析"
      >
        <Sparkles className="h-3 w-3 shrink-0 text-up" />
        {isPa ? (
          <PaBadgeLine entry={data} stopBefore={stopBefore} />
        ) : (
          <VerdictBadgeLine entry={data} />
        )}
        <span className="num ml-auto shrink-0 opacity-60">{dateLabel}</span>
      </button>

      {/* hover 浮窗(桌面; 向上弹出避免遮下方卡片价格区) */}
      <div className="absolute bottom-full left-0 z-20 mb-1 hidden w-64 group-hover:block">
        <div
          className="cursor-pointer rounded-md border border-border bg-bg-card p-3 text-left shadow-lg"
          onClick={() => setDrawerOpen(true)}
        >
          {isPa ? (
            <PaPopover entry={data} stopBefore={stopBefore} />
          ) : (
            <VerdictPopover entry={data} />
          )}
        </div>
      </div>

      <Drawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        title="最近分析"
      >
        <VerdictDetailCard verdict={data as unknown as Record<string, unknown>} />
      </Drawer>
    </div>
  );
}

// ── 徽标行(单行) ────────────────────────────────────────────────────────────

function VerdictBadgeLine({ entry }: { entry: LatestJournal }) {
  const v = entry.verdict ?? "";
  const color = verdictColor(v);
  const tone =
    color === "up" ? "text-up" : color === "down" ? "text-down" : "text-flat";
  const conf = entry.calibrated_confidence ?? entry.confidence;
  return (
    <>
      <span className={cn("font-medium", tone)}>{v || "—"}</span>
      {conf != null && <span className="opacity-80">校准 {conf}</span>}
    </>
  );
}

function PaBadgeLine({
  entry,
  stopBefore,
}: {
  entry: LatestJournal;
  stopBefore?: number | null;
}) {
  const pa = entry.position_action ?? {};
  const action = pa.action ?? "hold";
  const label = ACTION_LABELS[action] ?? action;
  const newStop = pa.new_stop ?? null;
  const dir =
    newStop != null && stopBefore != null && newStop !== stopBefore
      ? newStop > stopBefore
        ? "↑收紧"
        : "↓放宽"
      : null;
  return (
    <>
      <span className={cn("font-medium", actionTone(action))}>{label}</span>
      {newStop != null && (
        <span className="num opacity-80">
          止损 {formatPrice(newStop)}
          {dir && (
            <span className={cn("ml-0.5", dir === "↑收紧" ? "text-up" : "text-down")}>
              {dir}
            </span>
          )}
        </span>
      )}
    </>
  );
}

// ── hover 浮窗 ──────────────────────────────────────────────────────────────

function VerdictPopover({ entry }: { entry: LatestJournal }) {
  const pa = entry.price_advice ?? {};
  const evidence = (entry.evidence ?? []).slice(0, 2);
  const hasEntry =
    (pa.entry_low != null && pa.entry_high != null) || pa.entry != null;
  return (
    <div className="space-y-2 text-[11px]">
      <div className="flex items-center justify-between gap-2">
        <VerdictTag verdict={entry.verdict} />
        <span className="num text-text-secondary">
          {entry.confidence != null && `置信${entry.confidence}`}
          {entry.confidence != null && entry.calibrated_confidence != null && " / "}
          {entry.calibrated_confidence != null && `校准${entry.calibrated_confidence}`}
        </span>
      </div>
      <div className="num space-y-0.5 text-text-secondary">
        {hasEntry && (
          <p>
            入场{" "}
            {pa.entry_low != null && pa.entry_high != null
              ? `${formatPrice(pa.entry_low)}–${formatPrice(pa.entry_high)}`
              : formatPrice(pa.entry as number)}
          </p>
        )}
        {(pa.stop_loss != null || pa.target != null) && (
          <p>
            {pa.stop_loss != null && (
              <span className="text-down">止损 {formatPrice(pa.stop_loss)}</span>
            )}
            {pa.stop_loss != null && pa.target != null && " · "}
            {pa.target != null && (
              <span className="text-up">目标 {formatPrice(pa.target)}</span>
            )}
          </p>
        )}
      </div>
      {evidence.length > 0 && (
        <ul className="space-y-1 border-t border-border pt-2 text-text-primary">
          {evidence.map((ev, i) => (
            <li key={i} className="flex items-start gap-1">
              <span className="num text-flat">{i + 1}.</span>
              <span className="line-clamp-2">{ev}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="border-t border-border pt-1.5 text-flat">点击查看完整分析 →</p>
    </div>
  );
}

function PaPopover({
  entry,
  stopBefore,
}: {
  entry: LatestJournal;
  stopBefore?: number | null;
}) {
  const pa = entry.position_action ?? {};
  const action = pa.action ?? "hold";
  const label = ACTION_LABELS[action] ?? action;
  let sub = "";
  if (action === "add" && pa.add_shares != null) sub = `+${pa.add_shares}股`;
  else if (action === "trim" && pa.trim_shares != null) sub = `-${pa.trim_shares}股`;
  else if (action === "trim" && pa.trim_pct != null)
    sub = `-${Math.round(pa.trim_pct * 100)}%`;

  const newStop = pa.new_stop ?? null;
  const stopChanged = newStop != null && stopBefore != null && newStop !== stopBefore;
  const tighten = stopChanged && (newStop as number) > (stopBefore as number);
  const planCount = pa.scale_plan?.length ?? 0;

  return (
    <div className="space-y-2 text-[11px]">
      <div className="flex items-center justify-between gap-2">
        <span className={cn("text-sm font-semibold", actionTone(action))}>
          {label}
          {sub && <span className="num ml-1 text-xs font-normal">{sub}</span>}
        </span>
        <span className="num text-text-secondary">
          {(entry.analyzed_at ?? "").slice(5, 16)}
        </span>
      </div>
      <p className="num text-text-secondary">
        止损{" "}
        {stopChanged ? (
          <>
            {formatPrice(stopBefore as number)} → {formatPrice(newStop as number)}{" "}
            <span className={tighten ? "text-up" : "text-down"}>
              {tighten ? "↑收紧" : "↓放宽"}
            </span>
          </>
        ) : newStop != null ? (
          formatPrice(newStop)
        ) : (
          "维持"
        )}
      </p>
      {pa.rationale && (
        <div className="border-t border-border pt-2">
          <p className="mb-0.5 text-flat">核心判断</p>
          <p className="line-clamp-4 text-text-primary">{pa.rationale}</p>
        </div>
      )}
      <p className="border-t border-border pt-1.5 text-flat">
        {planCount > 0 ? `未来触发计划 ${planCount} 条 · ` : ""}点击查看全文 →
      </p>
    </div>
  );
}
```

`frontend/src/components/a-share/index.ts` 末尾加一行:

```ts
export { LatestAnalysisBadge } from "./LatestAnalysisBadge";
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/LatestAnalysisBadge.test.tsx`
Expected: 5 个测试全 PASS

- [ ] **Step 6: 类型检查 + Commit**

```bash
cd frontend && npx tsc --noEmit
cd .. && git add frontend/src/api/analyze.ts frontend/src/components/a-share/LatestAnalysisBadge.tsx frontend/src/components/a-share/index.ts frontend/src/components/a-share/__tests__/LatestAnalysisBadge.test.tsx
git commit -m "feat(frontend): LatestAnalysisBadge 最近分析徽标+hover浮窗组件

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 接入 CompactPositionCard

**Files:**
- Modify: `frontend/src/components/a-share/CompactPositionCard.tsx`(价格区下方插一行)
- Test: `frontend/src/components/a-share/__tests__/CompactPositionCard.test.tsx`(加 mock + 1 个用例)

**Interfaces:**
- Consumes: Task 1 的 `<LatestAnalysisBadge tsCode stopBefore />`。`stopBefore` 取 `position.plan?.last_stop_before ?? position.stop_loss`。

- [ ] **Step 1: 加失败测试**

`CompactPositionCard.test.tsx` 顶部(现有 `vi.mock("@/api/market", ...)` 之后)加:

```tsx
vi.mock("@/api/analyze", () => ({
  getLatestJournal: vi.fn(async () => null),
}));
```

describe 内加用例:

```tsx
it("最近分析徽标: 有 position_action journal -> 显示动作行", async () => {
  const { getLatestJournal } = await import("@/api/analyze");
  vi.mocked(getLatestJournal).mockResolvedValueOnce({
    ts_code: "600519.SH",
    analyzed_at: new Date().toISOString(),
    source: "position_action",
    position_action: { action: "trim", trim_shares: 100, new_stop: 95 },
  } as never);
  withClient(<CompactPositionCard position={pos} />);
  expect(await screen.findByText("减仓")).toBeTruthy();
});
```

注意:其余既有用例的 `getLatestJournal` 默认返回 null,徽标不渲染,不影响旧断言。

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/CompactPositionCard.test.tsx`
Expected: 新用例 FAIL(还没有徽标),旧用例全 PASS

- [ ] **Step 2: 卡片插入徽标**

`CompactPositionCard.tsx`:
- import 行 `import { PriceTag, ScalePlanLadder } from "@/components/a-share";` 改为 `import { PriceTag, ScalePlanLadder, LatestAnalysisBadge } from "@/components/a-share";`
- 在 `{/* 持仓盈亏 */}` 块结束(`</div>` 后的 `)}`)与 `{/* 参数行 */}` 前的 `<div className="my-2 border-t border-border" />` 之间插入:

```tsx
      {/* 最近 AI 分析徽标(hover 浮窗 / 点击 Drawer) */}
      <LatestAnalysisBadge
        tsCode={ts_code}
        stopBefore={position.plan?.last_stop_before ?? stop_loss}
      />
```

- [ ] **Step 3: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/CompactPositionCard.test.tsx`
Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/a-share/CompactPositionCard.tsx frontend/src/components/a-share/__tests__/CompactPositionCard.test.tsx
git commit -m "feat(frontend): 持仓卡接入最近分析徽标

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 接入 CompactCandidateCard

**Files:**
- Modify: `frontend/src/components/a-share/CompactCandidateCard.tsx`(价格区下方插一行)
- Test: `frontend/src/components/a-share/__tests__/CompactCandidateCard.test.tsx`(加 mock + 1 个用例)

**Interfaces:**
- Consumes: Task 1 的 `<LatestAnalysisBadge tsCode />`(候选卡不传 `stopBefore`)。

- [ ] **Step 1: 加失败测试**

先看 `CompactCandidateCard.test.tsx` 现有 mock 结构(它 mock 了 `@/api/market` 的 `getPrices`/`getDailyPrices`),在其后加:

```tsx
vi.mock("@/api/analyze", () => ({
  getLatestJournal: vi.fn(async () => null),
}));
```

describe 内加用例(candidate fixture 用文件里已有的那个):

```tsx
it("最近分析徽标: 有 verdict journal -> 显示方向+校准置信度", async () => {
  const { getLatestJournal } = await import("@/api/analyze");
  vi.mocked(getLatestJournal).mockResolvedValueOnce({
    ts_code: "600519.SH",
    analyzed_at: new Date().toISOString(),
    source: "verdict",
    verdict: "看多",
    calibrated_confidence: 62,
    price_advice: { entry: 12.5, stop_loss: 11.8, target: 14.5 },
  } as never);
  withClient(<CompactCandidateCard candidate={cand} />);
  expect(await screen.findByText("看多")).toBeTruthy();
  expect(screen.getByText(/校准\s*62/)).toBeTruthy();
});
```

(`cand` / `withClient` 换成该测试文件里实际的 fixture 名与 render helper 名;ts_code 与 fixture 一致。)

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/CompactCandidateCard.test.tsx`
Expected: 新用例 FAIL,旧用例全 PASS

- [ ] **Step 2: 卡片插入徽标**

`CompactCandidateCard.tsx`:
- import 行 `import { PriceTag } from "@/components/a-share";` 改为 `import { PriceTag, LatestAnalysisBadge } from "@/components/a-share";`
- 在 `{/* 距触发 */}` 块之后、`{/* 参数行 */}` 前的 `<div className="my-2 border-t border-border" />` 之前插入:

```tsx
      {/* 最近 AI 分析徽标(hover 浮窗 / 点击 Drawer) */}
      <LatestAnalysisBadge tsCode={ts_code} />
```

- [ ] **Step 3: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/components/a-share/__tests__/CompactCandidateCard.test.tsx`
Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/a-share/CompactCandidateCard.tsx frontend/src/components/a-share/__tests__/CompactCandidateCard.test.tsx
git commit -m "feat(frontend): 候选卡接入最近分析徽标

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 分析完成后失效 journalLatest 缓存

**Files:**
- Modify: `frontend/src/routes/analyze/AnalyzePage.tsx`(onVerdict 回调)

**Interfaces:**
- Consumes: `qk.journalLatest(tsCode)` / `qk.journal(tsCode)`(均已存在);`useQueryClient`(@tanstack/react-query)。
- Produces: 无新接口。效果:/analyze 页跑完分析后,回到概览/持仓页徽标立即是新数据。

- [ ] **Step 1: 修改 onVerdict 回调**

`AnalyzePage.tsx`:
- 顶部 import 区确认有 `useQueryClient`(没有则从 `@tanstack/react-query` 的现有 import 中补上)和 `qk`(已 import)。
- 组件内其他 hook 附近加:`const qc = useQueryClient();`
- 找到 `<AnalyzeTraceStream ... onVerdict={setLatestVerdict} ... />`(约 217 行),改为:

```tsx
              onVerdict={(v) => {
                setLatestVerdict(v);
                // 分析落 journal 后, 让卡片徽标/历史列表立即拿到新数据
                if (committedCode) {
                  qc.invalidateQueries({ queryKey: qk.journalLatest(committedCode) });
                  qc.invalidateQueries({ queryKey: qk.journal(committedCode) });
                }
              }}
```

- [ ] **Step 2: 类型检查 + 全量测试 + Commit**

```bash
cd frontend && npx tsc --noEmit && npx vitest run
```

Expected: tsc 无错误;62+ 既有测试 + 新增测试全 PASS

```bash
cd .. && git add frontend/src/routes/analyze/AnalyzePage.tsx
git commit -m "feat(frontend): 分析完成后失效 journalLatest 缓存, 卡片徽标即时刷新

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review 记录

- Spec 覆盖:徽标行(T1)、两种 kind 措辞(T1)、陈旧灰化(T1)、hover 浮窗两版(T1)、点击 Drawer 复用 VerdictDetailCard(T1)、两卡接入(T2/T3)、分析后缓存失效(T4)、零后端改动(全部前端)、YAGNI 项未引入。✓
- 类型一致性:`LatestAnalysisBadge` props `{ tsCode: string; stopBefore?: number | null }` 在 T1 定义,T2/T3 消费一致;`LatestJournal` 拓宽字段(evidence/confidence/entry_low/entry_high/add_shares/trim_shares/trim_pct)在 T1 Step 1 统一定义。✓
- 已知取舍:浮窗用 `hidden group-hover:block`,jsdom 里浮窗内容直接在 DOM 中,单测无需模拟 hover(测试注释已说明)。持仓卡插入点在盈亏行与参数行分隔线之间;候选卡在距触发行与分隔线之间。✓
