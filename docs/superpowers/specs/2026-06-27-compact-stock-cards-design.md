# 紧凑个股卡片 + 持仓/候选布局重构

日期: 2026-06-27
范围: 纯前端(`frontend/src`)。不动后端、不加新数据字段。

## 背景与问题

当前个股卡片是**行式列表**(每只股票占一整行,`divide-y`),三个痛点:

1. **太长浪费空间** —— 一行一只,纵向占用大。
2. **持仓和候选不能并列** —— 持仓页是上下堆叠两个 Card。
3. **金额看不懂、分不清哪个是当前价** —— `PriceTag` 把当前价本身也染红/绿,和涨跌额混在一起,主信息不突出。

## 设计目标

- 信息层次修正:**当前价 = 事实 → 中性黑色加粗;涨跌 = 评价 → 红/绿**。
- 网格化:一行 2-3 张竖向紧凑卡,纵向省空间。
- 持仓/候选在持仓页左右并列;概览页仅持仓满宽 3 列。

## 改动清单

### 1. `PriceTag` 改造(信息层次修正,全局生效)

文件: `frontend/src/components/a-share/PriceTag.tsx`

当前价本身被染 `directionClass`(红/绿)—— 这是"看不懂哪个是当前价"的根因。改成:

- **当前价 → `text-text-primary font-medium`**(黑色加粗),不再随涨跌染色。
- **涨跌额/幅 → `directionClass`**(红/绿),保持不变。
- 盘外「盘外·昨收」标注保留。

语义修正:事实中性、评价上色。候选卡、详情卡等所有调用方一并受益,无需逐个改。

具体:价格 `<span>` 的 `className` 去掉 `directionClass(delta)`,改为 `text-text-primary`。下方涨跌行不变。

### 2. 新增 `CompactPositionCard`(竖向紧凑卡)

文件: `frontend/src/components/a-share/CompactPositionCard.tsx`(新增)

替代行式 `PositionCard` 在网格里使用。结构:

```
┌──────────────────┐
│ 贵州茅台  价值     │  ← 名称(truncate) + 策略 tag
│ 600519.SH        │  ← 代码 灰小字
│                  │
│ 1750.00          │  ← 当前价 黑色加粗 text-text-primary text-lg(走 PriceTag)
│ ↑ +30  +1.74%    │  ← 涨跌额/幅 红/绿(PriceTag showChange)
│ ───────────────  │  ← 分隔线 border-b
│ 1000股 @ 1720    │  ← 股数@成本 灰小字(text-text-secondary text-[11px])
│ 🛡1680  🎯1850   │  ← 止损/目标 灰小字(ShieldAlert down色 / Target up色)
│ [加仓] [减仓]     │  ← onAdd/onReduce 可选
└──────────────────┘
```

Props 与现有 `PositionCard` 一致:`{ position, onAdd?, onReduce? }`。

复用现有价格查询(`getPrices`/`getDailyPrices` + `qk`)与 pnl 计算(`cost = avg_cost ?? entry_price`;`pnl = (current - cost) * shares`),只重排 JSX。

按钮显示规则:`onAdd`/`onReduce` 任一存在才渲染按钮行。概览页不传(只读),持仓页传(操作入口)。

pnl 行(盈亏 `+3000 元 (+1.74%)`)与现有 `PositionCard` 一致:`pnl >= 0 ? text-up : text-down`,带 `ArrowUpRight`/`ArrowDownRight`。放在涨跌行下方。

### 3. 新增 `CompactCandidateCard`(候选竖向卡)

文件: `frontend/src/components/a-share/CompactCandidateCard.tsx`(新增)

对齐持仓卡视觉,结构:

```
┌──────────────────┐
│ 隆基绿能  🔔已触发 │  ← 名称(truncate) + 触发态 tag(已触发才显示)
│ 601012.SH        │  ← 代码
│                  │
│ 22.15            │  ← 当前价 黑色加粗(PriceTag, showChange=false)
│ 距触发 -0.35 -1.56%│  ← 距触发距离(已触发时换"已触发"红字)
│ ───────────────  │  ← 分隔线
│ 触发 22.50(下方) │  ← 触发价+方向 灰小字
│ 🛡21.0  🎯25.0   │  ← 建议止损/目标 灰小字
└──────────────────┘
```

- 候选卡**无**加仓/减仓按钮(候选未持仓)。
- 距触发距离配色(取代原行式 `CandidateCard` 的逻辑):
  - **未触发 → 灰字**(`text-text-secondary`)
  - **接近触发(未触发但 `|distancePct| < 2%`)→ 黄字**(`text-amber` 或现有黄系)
  - **已触发 → 红字 + 整卡浅红底**(`bg-down/5` 或保持 `bg-up/5` 红涨惯例 —— 见下"待定")
- 已触发时距触发行替换为"已触发"红字提示。
- `prevClose` 传 `null`(候选不展示当日涨跌,只看距触发价)。

> **待定(实现时确认)**:整卡底色高亮色 —— 原 `CandidateCard` 用 `bg-up/5`(浅红,A 股红涨惯例,触发=利好)。新版保持 `bg-up/5`。距触发距离文字色单独处理(灰/黄/红)。即"卡片底色用 up 浅红表达已触发,文字色用灰/黄/红表达接近度"。

Props: `{ candidate }`(与现有 `CandidateCard` 一致)。

### 4. 概览页持仓列表 → 网格

文件: `frontend/src/routes/overview/OverviewPage.tsx`

持仓区域(line ~285 `positions.map(行式div)`)改成:

```tsx
<div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
  {positions.map((pos) => (
    <CompactPositionCard key={pos.ts_code} position={pos} />
  ))}
</div>
```

- 概览页**不传** `onAdd`/`onReduce`(只读,操作去持仓页)。
- 概览页**不加**候选。
- 删掉原行式 div 渲染逻辑(含其内的 `ShieldAlert`/`Target`/`PriceTag`/pnl 内联 JSX),由 `CompactPositionCard` 承担。`pnlMap`/`todayPnlMap` 用于顶部"今日盈亏汇总"卡,保留;不再用于单卡渲染(卡内自己算)。

### 5. 持仓页 → 持仓/候选左右并列

文件: `frontend/src/routes/watchlist/WatchlistPage.tsx`

现有"持仓 Card + 候选 Card 上下堆叠"改成左右并列:

```tsx
<div className="grid gap-4 lg:grid-cols-2">
  {/* 持仓 */}
  <Card>
    <CardHeader>...持仓 {n} 只...</CardHeader>
    <CardContent>
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
  </Card>

  {/* 候选 */}
  <Card>
    <CardHeader>...候选 {n} 只...</CardHeader>
    <CardContent>
      <div className="grid grid-cols-1 gap-2 xl:grid-cols-2">
        {data.candidates.map((c) => (
          <CompactCandidateCard key={c.ts_code} candidate={c} />
        ))}
      </div>
    </CardContent>
  </Card>
</div>
```

- `<lg` 屏幕(手机/窄屏)自动回退上下堆叠(`grid-cols-1`)。
- 持仓 Card 内部网格 `xl:grid-cols-2`(并列时每边 1-2 张)。
- 候选 Card 内部网格 `xl:grid-cols-2`。
- `onAdd`/`onReduce` 接现有 `openTradeForm`。

交易流水 Card、平仓复盘 Card 保持满宽不动(`grid` 之外)。

### 6. 旧组件删除

- 删除 `frontend/src/components/a-share/PositionCard.tsx`。
- 删除 `frontend/src/components/a-share/CandidateCard.tsx`。
- 更新 `frontend/src/components/a-share/index.ts` 导出:移除 `PositionCard`/`CandidateCard`,新增 `CompactPositionCard`/`CompactCandidateCard`。
- 全仓搜索确认无其他引用(`OverviewPage`/`WatchlistPage` 已改用新版)。

## 不在范围(YAGNI)

- 不动 `VerdictDetailCard` / journal / backtest 页。
- 不改后端。
- 不加新数据字段(股数/成本/止损/目标均现有字段)。

## 验证

- `python -c "import ast; ..."` 不适用(纯前端)。
- `cd frontend && npx tsc --noEmit` 通过(类型)。
- `cd frontend && npm run dev` 手动验证:
  - 概览页持仓一行 3 张,当前价黑色加粗,加仓/减仓按钮不出现。
  - 持仓页持仓/候选左右并列(宽屏),窄屏回退上下;持仓卡有加仓/减仓按钮。
  - 候选卡:未触发灰、接近触发黄、已触发红字+浅红底。
  - `PriceTag` 在所有调用处(候选/详情等)当前价均变黑色。
- `cd frontend && npm test` 既有 62 个单测不回归。
