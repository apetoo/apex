# 市场温度卡扩展(多指数 + 两市合计成交额)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把概览页"市场温度"卡从 2 指数扩展到 6 大指数 + 两市合计成交额(放量/缩量 + 昨日对比),并修复盘外标识失效与 `/index-daily` 漏解包两个 bug。

**Architecture:** 后端给指数日线加 `amount` 字段 + 新增批量端点 `/index-daily/batch`(包 `parse_json`)。前端新增批量 API + 2 个纯函数(`amountKToYi`/`formatVolume`,带单测),重写 `MarketIndexBar` 用一次请求拉 7 只指数(6 展示 + 深证综指算深市成交)。

**Tech Stack:** Python 3.12 / FastAPI / tushare `pro.index_daily` / React 19 / TypeScript 6 / TanStack Query v5 / Tailwind v4 / Vitest。

## Global Constraints

- **amount 单位千元**(tushare `pro.index_daily` 原单位);前端 `÷1e4` 转亿元,**先各自转亿元再相加**(避免大数精度)。
- **两市合计 = `000001.SH`(上证综指).amount + `399106.SZ`(深证综指).amount**。深市必须用综指 399106(全覆盖),**不能用成指 399001(仅 500 只,会严重偏低)**。399106 不显示成卡,仅取 amount。
- **批量共 7 只**(spec 写"8 只"是笔误,000001.SH 已在展示列表中复用):6 展示 + 399106.SZ。
- **6 展示指数**: `000001.SH` 上证 / `399001.SZ` 深成 / `399006.SZ` 创业板 / `000300.SH` 沪深300 / `000905.SH` 中证500 / `000688.SH` 科创50。
- **放量红 / 缩量绿**(用 `directionClass`,A 股红涨绿跌铁律);差额>0 放量、<0 缩量、=0 持平。
- **盘外判定**: 最新 bar 的 `trade_date !== 今天(YYYYMMDD 本地时区)` → 显示「盘外·`<周几>`收盘(`<MMDD>`)」。**不再用** `pct===0 && price===prevClose`(永远不触发)。
- **后端批量端点必须包 `parse_json`**(教训:`/index-daily` 曾漏解包,前端拿到字符串化 JSON 致卡空白)。
- **不显示单只指数成交量**(总量已在上方,单指数量冗余)。
- `get_index_daily` 单码函数/端点保留(向后兼容),`MarketIndexBar` 改用 batch。
- **VITE_USE_MOCK=0** 走真后端;mock 数据需含 `amount` 供 dev 可视化。
- **后端无测试套件**: 用 `python -c "import ast; ast.parse(open('<f>').read())"` + curl 烟测验证(CLAUDE.md 规约)。
- **前端验证**: `npx tsc -p tsconfig.app.json --noEmit`(根 `tsconfig.json` `files:[]` 是 no-op,必须用 `tsconfig.app.json`)+ `npm test` 不回归。
- **提交纪律**: **绝不** `git add -A` / `git add .` —— 工作树有大量无关 WIP;每个 commit 只 `git add` 明确文件。Commit message 末尾加 `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `apex/data.py` | `_fetch_index_bars` fields 加 amount;新增 `get_index_daily_batch` | 修改 |
| `backend/routers/market.py` | 新增 `/index-daily/batch` 路由(包 parse_json);drive-by 修 `/index-daily` 漏解包 | 修改 |
| `frontend/src/api/market.ts` | `IndexDailyBar.amount?`;`getIndexDailyBatch` + mock(7 只含 amount) | 修改 |
| `frontend/src/api/query-keys.ts` | 新增 `qk.indexDailyBatch(codes)` | 修改 |
| `frontend/src/lib/utils.ts` | 新增 `amountKToYi` + `formatVolume` 纯函数 | 修改 |
| `frontend/src/lib/__tests__/utils.test.ts` | 上述两函数单测 | 修改 |
| `frontend/src/components/a-share/MarketIndexBar.tsx` | 重写:6 指数网格 + 两市合计 + 盘外修正 | 修改 |

---

### Task 1: 后端 — 指数日线加 amount + 批量端点 + 修复 /index-daily 漏解包

**Files:**
- Modify: `apex/data.py:1035-1038`(`_fetch_index_bars` 的 tushare fields)
- Modify: `apex/data.py`(在 `get_index_daily` 后新增 `get_index_daily_batch`)
- Modify: `backend/routers/market.py:48-58`(drive-by 包 parse_json)+ 新增 batch 路由

**Interfaces:**
- Consumes: `apex/data.py` 现有 `_tushare()` / `json`;`backend/routers/market.py` 现有 `parse_json`(line 10 已 import)、`data` 模块。
- Produces:
  - `apex.data.get_index_daily_batch(codes: list[str], days: int = 2) -> str`(JSON 字符串 `{ [code]: { code, name, bars } }`,bars 含 `amount`)
  - `GET /api/market/index-daily/batch?codes=...&days=2` → `{ [code]: IndexDaily }`

**注意(working tree 状态):** `backend/routers/market.py` 的 `/index-daily` 路由(line 58)在上一会话已被改为 `return parse_json(data.get_index_daily(...))`,但**未提交**。本任务的 commit 会把它一起带进去(spec 要求"并入本特性提交")——这是预期的,不要回退。

- [ ] **Step 1: 给 `_fetch_index_bars` 的 fields 加 `amount`**

`apex/data.py:1035-1038` 当前:

```python
            df = pro.index_daily(
                ts_code=code, start_date=start, end_date=end,
                fields="trade_date,close,vol,pct_chg",
            )
```

改为:

```python
            df = pro.index_daily(
                ts_code=code, start_date=start, end_date=end,
                fields="trade_date,close,vol,pct_chg,amount",
            )
```

- [ ] **Step 2: 新增 `get_index_daily_batch`**

在 `apex/data.py` 的 `get_index_daily` 函数末尾(line 1071 `return json.dumps(payload, ensure_ascii=False)` 之后)插入:

```python
def get_index_daily_batch(codes: list[str], days: int = 2) -> str:
    """批量取指数日线, 返回 JSON 字符串 { [code]: { code, name, bars } }。

    用于市场温度卡一次拉多只指数。失败的 code 对应空 bars。
    bars 字段: trade_date / close / vol / pct_chg / amount(千元)。
    """
    INDEX_NAMES = {
        "000001.SH": "上证指数", "399001.SZ": "深证成指", "399006.SZ": "创业板指",
        "000300.SH": "沪深300", "000905.SH": "中证500", "000688.SH": "科创50",
        "399106.SZ": "深证综指",
    }
    bars_map = _fetch_index_bars(codes, days=days)
    result: dict[str, dict] = {}
    for c in codes:
        result[c] = {"code": c, "name": INDEX_NAMES.get(c, ""), "bars": bars_map.get(c, [])}
    return json.dumps(result, ensure_ascii=False)
```

- [ ] **Step 3: 新增 `/index-daily/batch` 路由(必须包 parse_json)**

在 `backend/routers/market.py` 的 `get_index_daily` 函数(line 48-58)之后插入新路由:

```python
@router.get("/index-daily/batch")
def get_index_daily_batch(
    codes: str = Query(..., description="逗号分隔指数代码"),
    days: int = Query(2, ge=1, le=30, description="最近 N 天"),
):
    """批量指数日线(ED13): 一次返回多只, 用于市场温度卡。

    返回 { [code]: { code, name, bars: [{ trade_date, close, vol, pct_chg, amount }] } }。
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    return parse_json(data.get_index_daily_batch(code_list, days=days))
```

- [ ] **Step 4: 语法检查**

Run: `python -c "import ast; ast.parse(open('apex/data.py').read()); ast.parse(open('backend/routers/market.py').read()); print('syntax ok')"`
Expected: `syntax ok`

- [ ] **Step 5: curl 烟测(需 uvicorn 在跑)**

Run:
```bash
curl -s 'http://127.0.0.1:8000/api/market/index-daily/batch?codes=000001.SH,399001.SZ,399006.SZ,000300.SH,000905.SH,000688.SH,399106.SZ&days=2' | python -m json.tool | head -30
```
Expected: 一个 JSON 对象,key 是 7 个指数代码,每只 `bars` 数组里每根 bar 含 `trade_date`/`close`/`vol`/`pct_chg`/`amount` 五个字段(`amount` 为数值,千元)。**body 是裸对象 `{...}`,不是带引号的字符串**。

同时验证单码端点已修复:
```bash
curl -s 'http://127.0.0.1:8000/api/market/index-daily?code=000001.SH&days=2' | head -c 40
```
Expected: 以 `{"code":` 开头(裸对象),不是 `"{\"code\":`。

- [ ] **Step 6: Commit**

```bash
git add apex/data.py backend/routers/market.py
git commit -m "feat(backend): 指数日线批量端点 + amount 字段, 修复 /index-daily 漏解包

- _fetch_index_bars fields 加 amount(成交额, 千元)
- 新增 get_index_daily_batch + GET /index-daily/batch(包 parse_json)
- drive-by: /index-daily 路由补 parse_json(曾漏解包致前端卡空白)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 前端 API 层 — 类型 + 批量函数 + mock + query key

**Files:**
- Modify: `frontend/src/api/market.ts:58-98`(类型 + mock + 新函数)
- Modify: `frontend/src/api/query-keys.ts`(新增 indexDailyBatch)

**Interfaces:**
- Consumes: `./client` 的 `api.get`;现有 `USE_MOCK`、`IndexDaily`。
- Produces:
  - `IndexDailyBar.amount?: number`(千元)
  - `IndexDailyMap = Record<string, IndexDaily>`
  - `getIndexDailyBatch(codes: string[], days?: number) => Promise<IndexDailyMap>`
  - `qk.indexDailyBatch(codes: string[])`

- [ ] **Step 1: `IndexDailyBar` 加 `amount?`,新增 `IndexDailyMap` 类型**

`frontend/src/api/market.ts:58-69` 当前:

```typescript
/** 指数日线(ED13): 返回 { code, name, bars: [{ trade_date, close, vol, pct_chg }] } */
export interface IndexDailyBar {
  trade_date: string;
  close: number;
  vol: number;
  pct_chg: number;
}
export interface IndexDaily {
  code: string;
  name: string;
  bars: IndexDailyBar[];
}
```

改为:

```typescript
/** 指数日线(ED13): 返回 { code, name, bars: [{ trade_date, close, vol, pct_chg, amount }] } */
export interface IndexDailyBar {
  trade_date: string;
  close: number;
  vol: number;
  pct_chg: number;
  amount?: number; // 成交额, 千元(tushare 原单位)
}
export interface IndexDaily {
  code: string;
  name: string;
  bars: IndexDailyBar[];
}
/** 批量指数日线: { [code]: IndexDaily } */
export type IndexDailyMap = Record<string, IndexDaily>;
```

- [ ] **Step 2: mock 数据补 amount + 扩到 7 只**

`frontend/src/api/market.ts:71-88` 的 `MOCK_INDEX_DAILY` 整体替换为(7 只,每根 bar 加 `amount`,单位千元):

```typescript
const MOCK_INDEX_DAILY: Record<string, IndexDaily> = {
  "000001.SH": {
    code: "000001.SH", name: "上证指数",
    bars: [
      { trade_date: "20260625", close: 4120.28, vol: 6.7e8, pct_chg: 0.23, amount: 6.5e7 },
      { trade_date: "20260626", close: 4027.26, vol: 6.6e8, pct_chg: -2.26, amount: 5.8e7 },
    ],
  },
  "399001.SZ": {
    code: "399001.SZ", name: "深证成指",
    bars: [
      { trade_date: "20260625", close: 16344.08, vol: 8.31e8, pct_chg: 1.82, amount: 7.2e7 },
      { trade_date: "20260626", close: 16000.0, vol: 8.1e8, pct_chg: -2.11, amount: 6.9e7 },
    ],
  },
  "399006.SZ": {
    code: "399006.SZ", name: "创业板指",
    bars: [
      { trade_date: "20260625", close: 2050.0, vol: 4.0e8, pct_chg: 2.1, amount: 3.5e7 },
      { trade_date: "20260626", close: 2000.0, vol: 3.9e8, pct_chg: -2.44, amount: 3.3e7 },
    ],
  },
  "000300.SH": {
    code: "000300.SH", name: "沪深300",
    bars: [
      { trade_date: "20260625", close: 4200.0, vol: 3.5e8, pct_chg: 0.8, amount: 4.0e7 },
      { trade_date: "20260626", close: 4150.0, vol: 3.4e8, pct_chg: -1.19, amount: 3.8e7 },
    ],
  },
  "000905.SH": {
    code: "000905.SH", name: "中证500",
    bars: [
      { trade_date: "20260625", close: 5500.0, vol: 2.8e8, pct_chg: 1.2, amount: 3.0e7 },
      { trade_date: "20260626", close: 5450.0, vol: 2.7e8, pct_chg: -0.91, amount: 2.9e7 },
    ],
  },
  "000688.SH": {
    code: "000688.SH", name: "科创50",
    bars: [
      { trade_date: "20260625", close: 980.0, vol: 1.2e8, pct_chg: 1.5, amount: 1.2e7 },
      { trade_date: "20260626", close: 965.0, vol: 1.1e8, pct_chg: -1.53, amount: 1.1e7 },
    ],
  },
  "399106.SZ": {
    code: "399106.SZ", name: "深证综指",
    bars: [
      { trade_date: "20260625", close: 1900.0, vol: 8.5e8, pct_chg: 1.7, amount: 7.5e7 },
      { trade_date: "20260626", close: 1860.0, vol: 8.3e8, pct_chg: -2.11, amount: 7.1e7 },
    ],
  },
};
```

(mock 验算:今日 SH 5.8e7千元=5800亿 + SZ 7.1e7千元=7100亿 = 12900亿 = 1.29万亿;昨日 6500+7500=14000亿=1.40万亿;差 -1100亿 ≈ -7.86% → 缩量绿。dev 可视化合理。)

- [ ] **Step 3: 新增 `getIndexDailyBatch`**

在 `frontend/src/api/market.ts` 末尾(`getIndexDaily` 之后)追加:

```typescript
export async function getIndexDailyBatch(
  codes: string[],
  days = 2,
): Promise<IndexDailyMap> {
  if (USE_MOCK) {
    const out: IndexDailyMap = {};
    codes.forEach((c) => {
      out[c] = MOCK_INDEX_DAILY[c] ?? { code: c, name: "", bars: [] };
    });
    return out;
  }
  return api.get<IndexDailyMap>(
    `/market/index-daily/batch?codes=${codes.join(",")}&days=${days}`,
  );
}
```

- [ ] **Step 4: `query-keys.ts` 加 `indexDailyBatch`**

`frontend/src/api/query-keys.ts` 的 `qk` 对象内(任意位置,建议 `intraday` 之后)加一行:

```typescript
  indexDailyBatch: (codes: string[]) => ["market", "index-daily", codes] as const,
```

- [ ] **Step 5: tsc 检查**

Run: `cd frontend && npx tsc -p tsconfig.app.json --noEmit`
Expected: 无输出(干净)。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/market.ts frontend/src/api/query-keys.ts
git commit -m "feat(frontend): 指数日线批量 API + amount 字段 + mock(7 只)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 前端纯函数 — amountKToYi + formatVolume(TDD)

**Files:**
- Modify: `frontend/src/lib/utils.ts`(末尾追加 2 个纯函数)
- Test: `frontend/src/lib/__tests__/utils.test.ts`(追加 2 个 describe)

**Interfaces:**
- Consumes: 无(纯函数)。
- Produces:
  - `amountKToYi(amtK: number | null | undefined): number | null` —— 千元 → 亿元(÷1e4);null/undefined/NaN → null
  - `formatVolume(yi: number | null | undefined): string` —— 亿元 → 显示串:`>=1e4` → `X.XX 万亿`,否则 `XXXX 亿`(整数);null/undefined/NaN → `—`

- [ ] **Step 1: 写失败测试(RED)**

在 `frontend/src/lib/__tests__/utils.test.ts` 顶部 import 加上 `amountKToYi, formatVolume`:

```typescript
import {
  cn,
  directionClass,
  formatPrice,
  formatPercent,
  formatRatio,
  formatDelta,
  amountKToYi,
  formatVolume,
} from "../utils";
```

在文件末尾追加:

```typescript
describe("amountKToYi(千元 → 亿元, ÷1e4)", () => {
  it("6.5e7 千元 → 6500 亿", () => {
    expect(amountKToYi(6.5e7)).toBe(6500);
  });
  it("1e4 千元 → 1 亿", () => {
    expect(amountKToYi(1e4)).toBe(1);
  });
  it("0 → 0", () => {
    expect(amountKToYi(0)).toBe(0);
  });
  it("null/undefined/NaN → null", () => {
    expect(amountKToYi(null)).toBeNull();
    expect(amountKToYi(undefined)).toBeNull();
    expect(amountKToYi(NaN)).toBeNull();
  });
});

describe("formatVolume(亿元 → 显示串)", () => {
  it("≥1e4 亿 → X.XX 万亿", () => {
    expect(formatVolume(12345)).toBe("1.23 万亿");
    expect(formatVolume(14000)).toBe("1.40 万亿");
    expect(formatVolume(10000)).toBe("1.00 万亿");
  });
  it("<1e4 亿 → XXXX 亿(整数)", () => {
    expect(formatVolume(1234.56)).toBe("1235 亿");
    expect(formatVolume(9999)).toBe("9999 亿");
    expect(formatVolume(0)).toBe("0 亿");
  });
  it("null/undefined/NaN → —", () => {
    expect(formatVolume(null)).toBe("—");
    expect(formatVolume(undefined)).toBe("—");
    expect(formatVolume(NaN)).toBe("—");
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/lib/__tests__/utils.test.ts`
Expected: FAIL —— `amountKToYi is not a function`(或 import 解析失败),新 describe 全红。

- [ ] **Step 3: 实现两个纯函数(GREEN)**

在 `frontend/src/lib/utils.ts` 末尾追加:

```typescript
/**
 * 成交额 千元 → 亿元(÷1e4)。
 * tushare index_daily 的 amount 单位是千元。
 * null/undefined/NaN → null(便于上层判空)。
 */
export function amountKToYi(amtK: number | null | undefined): number | null {
  if (amtK == null || Number.isNaN(amtK)) return null;
  return amtK / 1e4;
}

/**
 * 亿元 → 显示串: ≥1e4 亿显示「X.XX 万亿」, 否则「XXXX 亿」(整数)。
 * 用于两市合计成交额。null/undefined/NaN → —。
 */
export function formatVolume(yi: number | null | undefined): string {
  if (yi == null || Number.isNaN(yi)) return "—";
  if (yi >= 10000) return `${(yi / 10000).toFixed(2)} 万亿`;
  return `${yi.toFixed(0)} 亿`;
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/lib/__tests__/utils.test.ts`
Expected: PASS(全文件,含原有 + 新增 2 describe)。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/utils.ts frontend/src/lib/__tests__/utils.test.ts
git commit -m "feat(frontend): amountKToYi + formatVolume 纯函数(成交额格式化)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 前端重写 MarketIndexBar — 6 指数网格 + 两市合计 + 盘外修正

**Files:**
- Modify: `frontend/src/components/a-share/MarketIndexBar.tsx`(整体重写)

**Interfaces:**
- Consumes: Task 2 的 `getIndexDailyBatch`/`IndexDaily`/`qk.indexDailyBatch`;Task 3 的 `amountKToYi`/`formatVolume`;现有 `cn`/`directionClass`/`formatPercent`;`Card`/`CardContent`/`CardHeader`/`CardTitle`/`Button`(from `@/components/base`);lucide `TrendingUp`/`TrendingDown`/`Activity`/`RefreshCw`。
- Produces: 重写后的 `<MarketIndexBar />`(签名不变,无 props)。

- [ ] **Step 1: 整体重写 `MarketIndexBar.tsx`**

把 `frontend/src/components/a-share/MarketIndexBar.tsx` 全文替换为:

```tsx
import { useQuery } from "@tanstack/react-query";
import { TrendingUp, TrendingDown, Activity, RefreshCw } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, Button } from "@/components/base";
import { getIndexDailyBatch, type IndexDaily } from "@/api/market";
import { qk } from "@/api/query-keys";
import { cn, directionClass, formatPercent, amountKToYi, formatVolume } from "@/lib/utils";

/**
 * <MarketIndexBar> — 市场温度(6 大指数 + 两市合计成交额)
 *
 * 6 大指数: 上证/深成/创业板/沪深300/中证500/科创50
 * 两市合计成交额 = 000001.SH(上证综指) + 399106.SZ(深证综指) 的 amount
 *   注: 深市用综指 399106(全覆盖), 非成指 399001(仅 500 只, 会偏低)
 *   amount 单位千元, ÷1e4 转亿元后相加
 * 放量红/缩量绿(A 股红涨绿跌惯例)
 * 盘外: 最新 bar 的 trade_date ≠ 今天 → 「盘外·<周几>收盘(<MMDD>)」
 *
 * 数据源: /api/market/index-daily/batch(ED13, 一次拉 7 只)
 */
const DISPLAY_INDICES = [
  { code: "000001.SH", name: "上证指数" },
  { code: "399001.SZ", name: "深证成指" },
  { code: "399006.SZ", name: "创业板指" },
  { code: "000300.SH", name: "沪深300" },
  { code: "000905.SH", name: "中证500" },
  { code: "000688.SH", name: "科创50" },
] as const;

// 两市合计成交额的工具指数
const SH_COMP = "000001.SH"; // 沪市成交(上证综指, 已在 DISPLAY_INDICES 中复用)
const SZ_COMP = "399106.SZ"; // 深市成交(深证综指, 不展示)

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/** 本地今天的 YYYYMMDD(单用户工具, 本地时区够用) */
function todayStr(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}${m}${day}`;
}

/** "20260626" → "周五" */
function weekdayOf(yyyymmdd: string): string {
  const y = Number(yyyymmdd.slice(0, 4));
  const m = Number(yyyymmdd.slice(4, 6)) - 1;
  const d = Number(yyyymmdd.slice(6, 8));
  return WEEKDAYS[new Date(y, m, d).getDay()];
}

/** "20260626" → "0626" */
function mdOf(yyyymmdd: string): string {
  return yyyymmdd.slice(4, 8);
}

export function MarketIndexBar() {
  // 7 只: 6 展示 + 399106.SZ(深证综指, 仅取 amount)
  const allCodes = [...DISPLAY_INDICES.map((i) => i.code), SZ_COMP];
  const query = useQuery({
    queryKey: qk.indexDailyBatch(allCodes),
    queryFn: () => getIndexDailyBatch(allCodes),
  });
  const data: Record<string, IndexDaily | undefined> = query.data ?? {};

  const refetch = () => void query.refetch();

  // ── 两市合计成交额(亿元) ──
  const shBars = data[SH_COMP]?.bars ?? [];
  const szBars = data[SZ_COMP]?.bars ?? [];
  const shLatest = shBars[shBars.length - 1];
  const shPrevBar = shBars[shBars.length - 2];
  const szLatest = szBars[szBars.length - 1];
  const szPrevBar = szBars[szBars.length - 2];
  const shToday = amountKToYi(shLatest?.amount);
  const shPrev = amountKToYi(shPrevBar?.amount);
  const szToday = amountKToYi(szLatest?.amount);
  const szPrev = amountKToYi(szPrevBar?.amount);
  const todayTotal =
    shToday != null && szToday != null ? shToday + szToday : null;
  const prevTotal =
    shPrev != null && szPrev != null ? shPrev + szPrev : null;
  const diff =
    todayTotal != null && prevTotal != null ? todayTotal - prevTotal : null;
  const diffPct =
    diff != null && prevTotal !== 0 ? (diff / prevTotal) * 100 : null;
  const isVolume = diff != null && diff > 0; // 放量
  const isShrink = diff != null && diff < 0; // 缩量
  const volLabel = isVolume ? "放量" : isShrink ? "缩量" : "持平";

  // ── 盘外判定(最新 bar 日期 ≠ 今天) ──
  const latestDate = shLatest?.trade_date;
  const prevDate = shPrevBar?.trade_date;
  const isOutside = latestDate != null && latestDate !== todayStr();

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-text-secondary" />
          <CardTitle className="text-sm font-normal text-text-secondary">
            市场温度
          </CardTitle>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={refetch}
          aria-label="刷新指数"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
      </CardHeader>
      <CardContent className="pt-0">
        {query.isError ? (
          <p className="text-xs text-flat">行情异常</p>
        ) : (
          <>
            {/* 两市合计成交额 */}
            <div className="mb-3 border-b border-border/60 pb-3">
              <span className="text-xs text-text-secondary">两市合计成交</span>
              {todayTotal == null ? (
                <span className="num mt-1 block h-7 w-32 animate-pulse rounded bg-bg-base" />
              ) : (
                <div className="mt-1 flex items-baseline gap-2">
                  <span className="num text-xl font-semibold text-text-primary">
                    {formatVolume(todayTotal)}
                  </span>
                  {diff != null && (
                    <span
                      className={cn(
                        "num flex items-center gap-1 text-xs",
                        directionClass(diff),
                      )}
                    >
                      {isVolume ? (
                        <TrendingUp className="h-3 w-3" />
                      ) : isShrink ? (
                        <TrendingDown className="h-3 w-3" />
                      ) : null}
                      <span>{volLabel}</span>
                      <span>
                        {diff > 0 ? "+" : ""}
                        {diff.toFixed(0)} 亿
                      </span>
                      {diffPct != null && (
                        <span className="opacity-80">
                          {formatPercent(diffPct)}
                        </span>
                      )}
                    </span>
                  )}
                </div>
              )}
              {prevTotal != null && prevDate && (
                <p className="num mt-0.5 text-[10px] text-flat">
                  昨日 {formatVolume(prevTotal)} · {mdOf(prevDate)}
                </p>
              )}
            </div>

            {/* 6 大指数网格 */}
            <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
              {DISPLAY_INDICES.map((idx) => {
                const bars = data[idx.code]?.bars ?? [];
                const latest = bars[bars.length - 1];
                const price = latest?.close ?? null;
                const pct = latest?.pct_chg ?? null;
                const isUp = pct != null && pct > 0;
                const TrendIcon = isUp ? TrendingUp : TrendingDown;
                return (
                  <div key={idx.code} className="flex flex-col">
                    <span className="text-xs text-text-secondary">
                      {idx.name}
                    </span>
                    {price == null ? (
                      <span className="num mt-1 h-6 w-20 animate-pulse rounded bg-bg-base" />
                    ) : (
                      <span className="num mt-1 text-lg font-semibold text-text-primary">
                        {price.toFixed(2)}
                      </span>
                    )}
                    {pct != null && (
                      <div
                        className={cn(
                          "num mt-0.5 flex items-center gap-1 text-xs",
                          directionClass(pct),
                        )}
                      >
                        <TrendIcon className="h-3 w-3" />
                        <span>{formatPercent(pct)}</span>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            {isOutside && latestDate && (
              <p className="mt-3 text-[10px] text-flat">
                盘外·{weekdayOf(latestDate)}收盘({mdOf(latestDate)})
              </p>
            )}
          </>
        )}
        <p className="mt-3 text-[10px] text-flat">
          ED4 手动刷新 · 来自 /api/market/index-daily/batch(ED13)
        </p>
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: tsc 检查**

Run: `cd frontend && npx tsc -p tsconfig.app.json --noEmit`
Expected: 无输出(干净)。

- [ ] **Step 3: 既有单测不回归**

Run: `cd frontend && npm test`
Expected: 全部通过(注:`mutations.test.tsx` 有 2 个 `ERR_INVALID_URL` 是 base `c6176ac` 上就存在的预存失败,与本特性无关 —— 若只有这 2 个失败,视为不回归)。

- [ ] **Step 4: 手动验证(可选,需 dev server)**

`cd frontend && npm run dev`,打开概览页:
- 两市合计成交区:显示今日 `X.XX 万亿` + 放量/缩量标签 + 差额(亿)+ 百分比(放量红/缩量绿)+ 副行「昨日 X.XX 万亿 · MMDD」。
- 6 指数网格(`grid-cols-2`,宽屏 `sm:grid-cols-3`):每格 name + close(黑色加粗)+ pct(红绿),无单只成交量。
- 周六打开:底部显示「盘外·周五收盘(0626)」。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/a-share/MarketIndexBar.tsx
git commit -m "feat(frontend): MarketIndexBar 扩 6 指数 + 两市合计成交额 + 盘外修正

- 6 大指数网格(上证/深成/创业板/沪深300/中证500/科创50)
- 两市合计成交额: 000001.SH + 399106.SZ(深证综指) amount, 放量红/缩量绿 + 昨日对比
- 盘外判定改用 trade_date !== 今天(原 pct===0 判定永远不触发)
- 一次批量请求拉 7 只(替代 2 次单码请求)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 完成判据

- Task 1 curl 烟测:batch 端点返裸对象、7 只、bar 含 amount;单码端点已修复(裸对象)。
- Task 2/4:`tsc -p tsconfig.app.json --noEmit` 干净。
- Task 3:新纯函数单测全绿。
- Task 4:`npm test` 仅 2 个预存 `mutations` 失败(base 已有),无新增失败。
- 4 个 commit 在 v8 分支上,每个只 add 明确文件。
