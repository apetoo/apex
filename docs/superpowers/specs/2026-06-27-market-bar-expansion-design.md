# 市场温度卡扩展:多指数 + 两市合计成交额(放量/缩量)

日期: 2026-06-27
范围: 后端 `apex/data.py` + `backend/routers/market.py` + 前端 `frontend/src/api/market.ts` / `query-keys.ts` / `MarketIndexBar.tsx`。

## 背景与问题

概览页"市场温度"卡当前只有上证/深证 2 只指数,且:
1. **指数太少** —— 看不到创业板/沪深300/中证500/科创50 等大小盘结构。
2. **没有总量** —— 看不出今天大盘是缩量还是放量,也无法和昨天对比。
3. **盘外标识失效** —— `pct===0 && price===prevClose` 这套判定永远不触发(周末也有真实 pct_chg),导致周末不标「盘外」。

另:本次发现 `backend/routers/market.py` 的 `/index-daily` 端点漏调 `parse_json`(已在本轮修复 commit 中),导致前端拿到字符串化 JSON、卡空白。spec 确认该修复并入本特性。

## 设计目标

- 6 大指数卡片:上证综指 / 深证成指 / 创业板指 / 沪深300 / 中证500 / 科创50。
- 新增"两市合计成交额"区:今日合计 + 昨日合计 + 差额 + 百分比 + 放量/缩量文字标签(放量红/缩量绿,A 股红涨绿跌惯例)。
- 盘外标识改用 trade_date 判定(最新 bar 日期 ≠ 今天 → 盘外·<周几>收盘)。
- 后端新增批量端点,前端一次请求拉全部指数(避免 7 次 useQuery)。

## 数据源说明(关键)

两市合计成交额 = `000001.SH`(上证综指)的 `amount` + `399106.SZ`(**深证综指**)的 `amount`。

**必须用深证综指 399106.SZ,不能用深证成指 399001.SZ** —— 成指只含 500 只成分股,加进来深市成交会严重偏低。399106.SZ 作为"工具指数"参与拉取,但不显示成卡(展示用的深证指数仍是成指 399001.SZ)。

tushare `pro.index_daily` 的 `amount` 字段单位为**千元**。合计时先各自 ÷1e4 转成**亿元**,再相加,避免大数精度问题。

## 改动清单

### 1. 后端 `apex/data.py` — 指数日线加 amount 字段

`_fetch_index_bars`(line ~1022)的 tushare `fields` 加 `amount`:

```python
df = pro.index_daily(
    ts_code=code, start_date=start, end_date=end,
    fields="trade_date,close,vol,pct_chg,amount",
)
```

bars dict 原样透出 `amount`(千元,数值)。`get_index_daily` 单码函数不动(向后兼容,只是 bar 多了个 key)。

### 2. 后端 `backend/routers/market.py` — 新增批量端点

新增 `GET /api/market/index-daily/batch?codes=000001.SH,399001.SZ,...&days=2`,返回 `{ [code]: { code, name, bars } }`。

```python
@router.get("/index-daily/batch")
def get_index_daily_batch(
    codes: str = Query(..., description="逗号分隔指数代码"),
    days: int = Query(2, ge=1, le=30),
):
    """批量指数日线(ED13): 一次返回多只, 用于市场温度卡。
    返回 { [code]: { code, name, bars: [...] } }
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    return parse_json(data.get_index_daily_batch(code_list, days=days))
```

**必须包 `parse_json`**(本轮 bug 的教训)。

### 3. 后端 `apex/data.py` — 新增 `get_index_daily_batch`

```python
def get_index_daily_batch(codes: list[str], days: int = 2) -> str:
    """批量取指数日线, 返回 JSON 字符串 { [code]: { code, name, bars } }。
    失败的 code 对应空 bars。"""
    INDEX_NAMES = {"000001.SH": "上证指数", "399001.SZ": "深证成指",
                   "399006.SZ": "创业板指", "000300.SH": "沪深300",
                   "000905.SH": "中证500", "000688.SH": "科创50",
                   "399106.SZ": "深证综指"}
    bars_map = _fetch_index_bars(codes, days=days)
    result = {}
    for c in codes:
        result[c] = {"code": c, "name": INDEX_NAMES.get(c, ""), "bars": bars_map.get(c, [])}
    return json.dumps(result, ensure_ascii=False)
```

### 4. 前端 `frontend/src/api/market.ts` — 类型 + 批量函数

`IndexDailyBar` 加 `amount?: number`(千元,可选兼容)。

新增批量类型与函数:

```typescript
export interface IndexDailyBar {
  trade_date: string;
  close: number;
  vol: number;
  pct_chg: number;
  amount?: number; // 千元(后端 tushare 原单位)
}
export type IndexDailyMap = Record<string, IndexDaily>;

export async function getIndexDailyBatch(
  codes: string[],
  days = 2,
): Promise<IndexDailyMap> {
  if (USE_MOCK) {
    // mock: 给每只造 2 根 bar, 含 amount(千元), 用于联调可视化
    const out: IndexDailyMap = {};
    codes.forEach((c, i) => {
      out[c] = MOCK_INDEX_DAILY[c] ?? {
        code: c, name: "",
        bars: [
          { trade_date: "20260625", close: 1000 + i, vol: 5e8, pct_chg: 0.5, amount: 6e8 },
          { trade_date: "20260626", close: 1010 + i, vol: 5.5e8, pct_chg: 1.0, amount: 6.6e8 },
        ],
      };
    });
    return out;
  }
  return api.get<IndexDailyMap>(
    `/market/index-daily/batch?codes=${codes.join(",")}&days=${days}`,
  );
}
```

mock `MOCK_INDEX_DAILY` 的现有 2 只 bar 也补 `amount` 字段(千元),保持结构一致。

### 5. 前端 `frontend/src/api/query-keys.ts` — 统一 index key

```typescript
indexDailyBatch: (codes: string[]) => ["market", "index-daily", codes] as const,
```

`MarketIndexBar` 改用 `qk.indexDailyBatch(codes)`,替换内联 `["index-daily", code]`。

### 6. 前端 `MarketIndexBar.tsx` — 重写

**指数清单**(展示用 6 只):
```typescript
const DISPLAY_INDICES = [
  { code: "000001.SH", name: "上证指数" },
  { code: "399001.SZ", name: "深证成指" },
  { code: "399006.SZ", name: "创业板指" },
  { code: "000300.SH", name: "沪深300" },
  { code: "000905.SH", name: "中证500" },
  { code: "000688.SH", name: "科创50" },
];
// 工具指数(深证综指, 仅取 amount 算两市合计, 不显示)
const SH_COMP = "000001.SH";  // 沪市成交
const SZ_COMP = "399106.SZ";  // 深市成交(综指, 非成指)
```

一次 `useQuery`(`qk.indexDailyBatch([...6 展示, SH_COMP, SZ_COMP])`)拉 8 只。

**两市合计成交额计算**:
```typescript
// amount 千元 → 亿元
const toYi = (amt?: number) => (amt != null ? amt / 1e4 : null);
const shToday = toYi(shBars[shBars.length-1]?.amount);
const shPrev  = toYi(shBars[shBars.length-2]?.amount);
const szToday = toYi(szBars[szBars.length-1]?.amount);
const szPrev  = toYi(szBars[szBars.length-2]?.amount);
const todayTotal = shToday != null && szToday != null ? shToday + szToday : null;
const prevTotal  = shPrev  != null && szPrev  != null ? shPrev  + szPrev  : null;
const diff = todayTotal != null && prevTotal != null ? todayTotal - prevTotal : null;
const diffPct = diff != null && prevTotal !== 0 ? (diff / prevTotal) * 100 : null;
const isVolume = diff != null && diff > 0;   // 放量
const isShrink = diff != null && diff < 0;   // 缩量
```

**展示结构**:

```
┌─────────────────────────────────────────────────────┐
│ ⚡ 市场温度                              [刷新]      │
├─────────────────────────────────────────────────────┤
│  两市合计成交                                        │
│  1.23 万亿   ↑ 放量 +1234 亿 (+11.2%)  ← 红(放量)   │
│  昨日 1.10 万亿 · 0626                               │
├─────────────────────────────────────────────────────┤
│  上证指数     深证成指     创业板指                  │  ← grid-cols-2 sm:grid-cols-3
│  4027.26      16344.08    ...                        │  ← close 黑色加粗(text-text-primary)
│  -2.26%       +1.82%      ...                        │  ← pct 红绿
│  沪深300      中证500     科创50                     │
│  ...          ...         ...                        │
├─────────────────────────────────────────────────────┤
│  盘外·周五收盘(0626)            ED4 手动刷新         │
└─────────────────────────────────────────────────────┘
```

- **合计成交区**:今日合计(亿/万亿自适应:`>=1e4` 亿显示 `X.XX 万亿`,否则 `XXXX 亿`)+ 趋势箭头 + `放量`/`缩量` 文字 + 差额(亿)+ 百分比,整行 `directionClass(diff)`(放量红/缩量绿)。副行:昨日合计 + 日期。
- **6 指数网格**:`grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3`。每格 name(灰小字)+ close(`text-text-primary font-semibold text-lg`)+ pct(`directionClass` + TrendingUp/Down 图标)。**不再显示单只指数的成交量**(总量已在上方)。
- **盘外标识(修正)**:`const today = new Date()` → `YYYYMMDD`;`latest.trade_date !== todayStr` → 显示「盘外·`<周几>`收盘(`<trade_date 后4位 月日>`)」。例如周六看周五数据 → 「盘外·周五收盘(0626)」。注意:浏览器本地时区,够用(单用户工具)。

### 7. drive-by:`backend/routers/market.py:58` 已包 `parse_json`

(本轮发现并已修复的 `/index-daily` 漏解包,并入本特性提交。)

## 不在范围(YAGNI)

- 不加分时/资金流向/北向资金/板块涨跌。
- 不动概览页其它卡(持仓网格/今日盈亏/总资产)。
- 不改 `parse_json` 那句误导注释(上次确认留着)。
- 不为单只指数显示成交量(总量已足够,单指数量冗余)。

## 验证

- 后端:
  - `python -c "import ast; ast.parse(open('apex/data.py').read()); ast.parse(open('backend/routers/market.py').read())"`
  - `curl 'http://127.0.0.1:8000/api/market/index-daily/batch?codes=000001.SH,399001.SZ,399006.SZ,000300.SH,000905.SH,000688.SH,399106.SZ&days=2'` → 7 只,每只 bars 含 `amount`。
- 前端:
  - `cd frontend && npx tsc -p tsconfig.app.json --noEmit` 通过。
  - `cd frontend && npm test` 既有单测不回归(MarketIndexBar 当前无单测,不新增;改的是展示逻辑,风险低)。
  - 手动:`npm run dev` 看卡 —— 6 指数网格 + 两市合计(放量红/缩量绿 + 差额 + pct + 昨日)+ 周六显示「盘外·周五收盘」。
