# 最近分析徽标 + hover 浮窗(持仓/候选卡)

日期:2026-07-30
状态:已与用户对齐,待实现

## 背景与目标

持仓卡(`CompactPositionCard`)和候选卡(`CompactCandidateCard`)目前只有行情与交易参数,看不到最近一次 AI 分析的方向/动作/理由。用户要在卡片上低成本地"扫一眼核心判断,想看全文再点开"。

已定交互:**A(常驻徽标行)+ B(hover 浮窗)混合**。浮窗重点:持仓 = 加减仓动作 + 核心判断;候选 = 方向 + 三价 + 证据。

## 现状地基(复用,不重建)

- `GET /api/journal/{ts_code}/latest` 已存在,返回最近一条 entry;`source === "position_action"` 区分加减仓建议 vs 方向 verdict(前端 `getLatestJournal` 已封装)。
- `VerdictDetailCard` 是共享组件,/journal 页已有"点击行 → Drawer 弹出完整分析"模式,本次复用同一组件与交互。
- base 组件层无 popover;浮窗用纯 CSS 实现,不引入定位库。

## 设计

### 1. 常驻徽标行(两卡通用)

位置:价格区下方。单行小字,按最新 entry 的 kind 自动换措辞:

| 场景 | 文案 |
|---|---|
| 持仓,最新 = position_action | `✦ 减仓 · 止损12.30→12.80 ↑ · 07-28` |
| 持仓,最新 = verdict | `✦ 看多 · 校准62 · 07-28` |
| 候选,最新 = verdict | `✦ 看多 · 校准62 · 07-28` |

- 配色:加仓/看多红、减仓/清仓/看空绿、持有/中性灰(沿用 `LastAdviceRow` 的 tone 约定,红涨绿跌铁律)。
- 陈旧度:距今 > 5 天整行灰化,日期改显 `N 天前`,暗示该重分析。
- 无 journal(纯手动持仓)→ 整行不渲染,不占位,无 layout shift(loading 期同样不渲染)。
- 点击徽标行 → 右侧 Drawer,复用 `VerdictDetailCard` 渲染完整 entry。

### 2. hover 浮窗(悬浮徽标行触发)

**持仓浮窗**(最新 = position_action):

```
┌─────────────────────────────────┐
│ 减仓 −300股            07-28 14:30│
│ 止损 12.30 → 12.80  ↑收紧        │
│─────────────────────────────────│
│ 核心判断                          │
│ <rationale, clamp 4 行>          │
│─────────────────────────────────│
│ 未来触发计划 2 条 · 点击查看全文 → │
└─────────────────────────────────┘
```

- 持仓最新一条是 verdict 时(手动持仓只跑过方向分析),浮窗退化为候选同款。

**候选浮窗**(最新 = verdict):

```
┌─────────────────────────────────┐
│ 看多  置信68 / 校准62      07-28  │
│ 入场 12.30–12.60                 │
│ 止损 11.80 · 目标 14.50          │
│─────────────────────────────────│
│ ① <evidence[0]>                  │
│ ② <evidence[1]>                  │
│─────────────────────────────────│
│ 点击查看完整分析 →                │
└─────────────────────────────────┘
```

- 证据链只取前 2 条,全文在 Drawer。
- 浮窗向上弹出(absolute bottom-full),避免网格下方卡片遮住价格区。

### 3. 技术方案

- **新组件 `LatestAnalysisBadge`**(`components/a-share/`):徽标行 + CSS `group-hover` 浮窗 + Drawer 接线三合一。`CompactPositionCard` / `CompactCandidateCard` 各插一行调用,卡片本身改动极小。
- **浮窗**:纯 CSS `group-hover` + `absolute bottom-full`,无 JS 定位;`pointer-events` 保证浮窗内可点击。移动端无 hover,自然退化为点击 → Drawer,零额外代码。
- **数据**:每卡 `useQuery(getLatestJournal(ts_code))`,react-query 按 ts_code 缓存去重(多卡同 code 只发一次),`staleTime: 5min`。分析跑完(/analyze 页 done)后 invalidate 对应 key,让徽标刷新。
- **零后端改动**。

### 4. 边界情况

- entry 缺字段(老数据无 calibrated_confidence / evidence / rationale)→ 对应行不渲染,不显示 "null"。
- `trigger_direction`/action 未知值 → 原样显示,不崩。
- Drawer 打开期间浮窗关闭(hover 态与点击态互斥,避免叠层)。

## 测试

- 组件单测(Vitest + testing-library):
  - verdict kind:徽标文案(方向 + 校准置信度 + 日期)、浮窗含三价与前 2 条证据
  - position_action kind:徽标文案(action + 止损变动)、浮窗含 rationale
  - 陈旧(>5 天)灰化 + "N 天前"
  - 无 journal → 不渲染
  - 点击徽标 → Drawer 打开
- mock `getLatestJournal` via fetchFn/查询 client 注入,沿用现有测试风格。

## 明确不做(YAGNI)

- 后端批量内嵌 summary(watchlist 响应改造)——先 N 卡 N 请求 + 缓存,量大了再说。
- 浮窗内放 K 线/分时图。
- JS 定位库 / 第三方 popover 组件。
- 多页共用触发(push 通知、chat 面板等)——本次只上两张卡。
