# 手动持仓 + 买卖留痕 设计

- 日期: 2026-06-27
- 分支: v8
- 范围: 用户可手动维护持仓(数量/成本/现价),并记录买入/卖出操作留痕,供未来 AI 操作诊断。

## 背景

当前持仓模型是单 lot:每个 ts_code 一条 `active_positions` 记录,字段 `entry_price` + `position_size_shares`,只有"平仓"动作(`close_position` → 写 `closed_positions.jsonl` + 触发 postmortem + calibration)。用户无法:

1. 手动加持仓(买入新代码)——后端有 `POST /positions` 但 UI 只暴露"加候选",且 `stop_loss`/`target` 必填,不适合纯手动录入。
2. 加仓 / 减仓——只能整笔平仓。
3. 留痕买卖——`closed_positions.jsonl` 只记录整笔往返,中间的买入/卖出无流水。

## 目标

- 用户能在持仓页手动开仓(代码 + 成本价 + 股数)。
- 能对已有持仓加仓、减仓。
- 每笔买卖写入 `trades.jsonl` 留痕,供未来 AI 诊断操作节奏。
- 复用现有 `close_position` / postmortem / calibration 反哺三线,不破坏时序。

## 非目标

- AI 读 `trades.jsonl` 做操作诊断——独立后续 spec。
- 多账户、多币种、手续费/印花税精细核算(本轮 amount = fill_price × shares,不扣费)。

## 决策

| 决策点 | 选择 |
|---|---|
| 数据模型 | **持仓为主 + 旁路交易流水**。`active_positions` 随买卖变动,`trades.jsonl` 每笔留痕,`closed_positions.jsonl` 不变。 |
| 成本价字段 | **新增 `avg_cost`,保留 `entry_price`**。`entry_price`=首次开仓价(不变),`avg_cost`=加仓后均价(随买入变动)。PnL 一律用 `avg_cost`。老数据迁移 `avg_cost=entry_price`。 |
| 减仓卖光 | **自动触发复盘**。卖出股数 ≥ 持有时,追加 sell trade 后走 `close_position`(postmortem+calibration),从 active_positions 移除。卖出 < 持有时减仓留痕,不复盘。 |
| 开仓必填 | **只必填 代码 + 成本价 + 股数**。止损/目标可选,缺则不写 key。`strategy` 默认 `'manual'`。名称反查 tushare。 |
| 范围 | **只做留痕基础设施**,AI 诊断后续。 |

## 数据模型

### `active_positions`(扩展,单持仓每 ts_code 不变)

| 字段 | 语义 | 变动 |
|---|---|---|
| `ts_code` | 股票代码 | 不变 |
| `name` | 名称 | 不变 |
| `entry_price` | 首次开仓价 | 开仓后不再变 |
| `avg_cost` | 加仓后加权均价 | **新增**;开仓时 = entry_price,加仓重算,减仓不变 |
| `position_size_shares` | 当前总股数 | 买加卖减 |
| `stop_loss` / `target` | 止损/目标 | **改可选**,缺则不写 key |
| `entry_date` | 首次开仓日 | 不变 |
| `strategy` / `risk_amount` / `calibrated_confidence` / `regime_at_open` / `trigger_*` / `expires_at` / `status` | 原有 | 不变 |

老数据迁移:`avg_cost` 缺失 → `avg_cost = entry_price`(在 `migrate_and_backfill` 里补)。

### `trades.jsonl`(新增,`~/.stock-journal/trades.jsonl`)

追加式,每笔买卖一行,JSONL。供未来 AI 诊断。字段:

```jsonc
{
  "trade_id": "20260627T143012-0001",      // 时间戳基 + 自增,排序/去重用
  "ts_code": "002050.SZ",
  "name": "三花智控",
  "side": "buy",                            // buy | sell
  "fill_price": 12.50,                      // 实际成交价
  "shares": 1000,                           // 本笔股数
  "amount": 12500.0,                        // fill_price × shares
  "realized_pnl": null,                     // buy=null;sell=(fill_price - avg_cost_before) × shares
  "realized_pnl_pct": null,                 // sell 才有:(fill_price/avg_cost_before - 1)
  "avg_cost_after": 12.50,                  // 本笔后的最新均价
  "shares_after": 1000,                     // 本笔后的总股数
  "strategy": "manual",                     // 继承自持仓 / 开仓默认 manual
  "regime": "大盘震荡",                      // 下单时今日 regime(无则 null)
  "journal_ref": {                          // 下单时该股最近 AI verdict(无则 null)
    "verdict": "bullish", "confidence": 0.72,
    "analyzed_at": "2026-06-26T10:00:00"
  },
  "note": "财报后加仓",
  "traded_at": "2026-06-27T14:30:12+08:00"
}
```

### `closed_positions.jsonl`(形状不变)

`close_position` 的实现 pnl 基准改用 `avg_cost`(而非 `entry_price`)。其余字段不变。

## apex 服务层

### 新模块 `apex/trades.py`

把交易流水审计与持仓状态分离,职责清晰(留痕读写独立于持仓状态机)。

- `_path()` → `~/.stock-journal/trades.jsonl`
- `_next_trade_id(now)` → `"YYYYMMDDTHHMMSS-<4位序号>"`(同秒多笔递增;无 Math.random/Date.now 限制——这是普通 Python 运行时,不受 workflow 脚本约束)
- `append_trade(**fields)` → 组装 dict + `json.dumps` 追加一行,返回该 dict
- `load_trades(ts_code=None, limit=None, since_days=None) -> list[dict]` → 读 JSONL,按 `traded_at` 倒序,可按 ts_code / limit / since_days 过滤

### `apex/watchlist.py` 改动

**`add_position`**:
- 新增写入 `avg_cost = entry_price`
- `stop_loss` / `target` 参数改 Optional,缺则不写 key(向后兼容老调用)

**新增 `buy(ts_code, fill_price, shares, stop_loss=None, target=None, note="", strategy="manual", regime=None)`**:

```
校验 fill_price>0, shares>0
load wl
existing = active_positions 里找 ts_code
取 regime(传参 or 今日 regime 缓存,无则 None)
取 journal_ref(该股最近一条 journal entry,无则 None)
if existing:
    加仓:
      old_shares = existing.position_size_shares or 0
      old_avg    = existing.avg_cost or existing.entry_price or 0
      new_shares = old_shares + shares
      new_avg    = (old_avg×old_shares + fill_price×shares) / new_shares   # old_shares=0 时 = fill_price
      更新 existing.position_size_shares, avg_cost
      若传了 stop_loss/target 则更新(不传不动)
      若 strategy 仍是默认 manual 而持仓已有非 manual strategy,不动
else:
    开仓:
      name = 反查 tushare(失败空串)
      建 record: entry_price=avg_cost=fill_price, position_size_shares=shares,
                 entry_date=今日, strategy=strategy, 可选 stop_loss/target, status=active
      append active_positions
save wl
append_trade(side=buy, fill_price, shares, amount, realized_pnl=None,
             avg_cost_after=new_avg, shares_after=new_shares,
             strategy, regime, journal_ref, note)
返回 {"position": <更新后 record>, "trade": <写入的 trade>}
```

**新增 `sell(ts_code, fill_price, shares, exit_reason="manual", note="", postmortem=True)`**:

```
校验 fill_price>0, shares>0
load wl
pos = active_positions 里找 ts_code;无 → PositionNotFoundError
holding = pos.position_size_shares or 0
if shares > holding → ValueError("卖出股数 超过持有 holding")
avg_cost_before = pos.avg_cost or pos.entry_price
取 regime, journal_ref(同 buy)
if shares >= holding:
    # 卖光 → 留痕 + 走 close_position
    realized = (fill_price - avg_cost_before) × holding
    append_trade(side=sell, fill_price, shares=holding, realized_pnl=realized, ...)
    rec = close_position(ts_code, exit_price=fill_price, exit_reason=exit_reason,
                         actual_fill_price=avg_cost_before)   # pnl 基准正确
    if postmortem:
        diagnosis = pm.run_and_patch(rec); cal.compute()  # 失败不阻断
    返回 {"trade": <sell trade>, "closed_record": rec, "diagnosis": diagnosis}
else:
    # 减仓 → 留痕 + 改状态,不复盘
    realized = (fill_price - avg_cost_before) × shares
    new_shares = holding - shares
    pos.position_size_shares = new_shares   # avg_cost 不变
    save wl
    append_trade(side=sell, fill_price, shares, realized_pnl=realized,
                 realized_pnl_pct=(fill/avg_cost_before - 1),
                 avg_cost_after=avg_cost_before, shares_after=new_shares, ...)
    返回 {"position": <更新后 pos>, "trade": <sell trade>}
```

**`close_position`**:
- `fill_price = actual_fill_price if actual_fill_price is not None else (pos.avg_cost or pos.entry_price)`(改为优先 avg_cost)
- `realized_pnl_pct` 基准用 fill_price(已是 avg_cost)

**`migrate_and_backfill`**:
- 在现有 normalize/name 循环里,对 `active_positions` 补 `avg_cost`(缺失则 = `entry_price`)

> 注意:本轮 sell 卖光路径调用 close_position,后者会再 append closed_positions.jsonl——但**不会**再写 trade(trade 由 sell 自己写一次)。close_position 不动 trades.jsonl。

## backend 路由 + schemas

### `backend/schemas/watchlist.py`

```python
class BuyRequest(BaseModel):
    ts_code: str
    fill_price: float
    shares: int
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    note: str = ""
    strategy: str = "manual"

class SellRequest(BaseModel):
    ts_code: str
    fill_price: float
    shares: int
    exit_reason: str = "manual"
    note: str = ""
    postmortem: bool = True
```

`AddPositionRequest.stop_loss` / `target` 改 Optional(向后兼容;router 现有 add_position 调用不变,只是允许空)。

### `backend/routers/watchlist.py` 新增

```python
@router.post("/buy")
def buy(req: BuyRequest):
    ts_code = normalize_ts_code(req.ts_code)
    return wl.buy(ts_code, req.fill_price, req.shares,
                  stop_loss=req.stop_loss, target=req.target,
                  note=req.note, strategy=req.strategy)

@router.post("/sell")
def sell(req: SellRequest):
    ts_code = normalize_ts_code(req.ts_code)
    return wl.sell(ts_code, req.fill_price, req.shares,
                   exit_reason=req.exit_reason, note=req.note,
                   postmortem=req.postmortem)

@router.get("/trades")
def list_triggers(ts_code: Optional[str]=None,
                  limit: Optional[int]=Query(None, ge=1, le=1000),
                  since_days: Optional[int]=Query(None, ge=1, le=3650)):
    if ts_code: ts_code = normalize_ts_code(ts_code)
    return trades.load_trades(ts_code=ts_code, limit=limit, since_days=since_days)
```

异常映射复用 `core/errors.py`:ValueError→400、PositionNotFoundError→404。路由无 try/except(对齐现有约定)。

## frontend

### `api/watchlist.ts`
- `buy(req)` / `sell(req)` / `getTrades(params?)`
- `ActivePosition` 类型加 `avg_cost?: number`
- 新增 `Trade` 类型(trade_id/ts_code/name/side/fill_price/shares/amount/realized_pnl/realized_pnl_pct/avg_cost_after/shares_after/strategy/note/traded_at)

### `api/mutations.ts`
- `useBuy`:onSuccess 失效 watchlist + trades
- `useSell`:onSuccess 失效 watchlist + trades;若返回含 `closed_record`(卖光),再失效 closed + calibration

### `api/query-keys.ts`
- 加 `trades: { all: ['trades'], byCode: (c)=>['trades', c], list: (c?)=>c?['trades',c]:['trades'] }`

### `routes/watchlist/WatchlistPage.tsx`
- 持仓区顶部加 **"加持仓"** 按钮 → 手动开仓表单(代码*、成本价*、股数*、止损、目标、备注)→ `buy()`
- 每张 `PositionCard` 加 **加仓** / **减仓** 按钮 → 内联表单(股数、成交价、备注)→ `buy()` / `sell()`
- 新增 **"交易流水"** Card:`getTrades({limit:50})` 列近期 trades——side 标签(买红/卖绿)、名称+代码、成交价、股数、实现 pnl(若有)、时间、备注

### `components/a-share/PositionCard.tsx`
- PnL 基准改 `avg_cost ?? entry_price`
- 展示股数 + 均价(`avg_cost`);保留止损/目标展示(可选)

## 错误处理 & 验证

- 复用现有异常映射,无新 boilerplate
- 校验在 `buy`/`sell` 内:`fill_price>0`、`shares>0`、卖出不超持有
- 无测试套件(CLAUDE.md 约定):`python -c "import ast; ast.parse(open(f).read())"` 验语法 + tempdir smoke-test `apex.trades.append_trade/load_trades`、`wl.buy`(开仓+加仓)、`wl.sell`(减仓+卖光,postmortem=false 避免真实 AI 调用)

## 影响面

| 文件 | 改动 |
|---|---|
| `apex/trades.py` | 新建 |
| `apex/watchlist.py` | add_position 加 avg_cost/可选止损目标;新增 buy/sell;close_position pnl 基准;迁移补 avg_cost |
| `backend/schemas/watchlist.py` | 新增 Buy/SellRequest;AddPosition 止损目标改可选 |
| `backend/routers/watchlist.py` | 新增 /buy /sell /trades;import trades 模块 |
| `frontend/src/api/watchlist.ts` | buy/sell/getTrades;avg_cost;Trade 类型 |
| `frontend/src/api/mutations.ts` | useBuy/useSell |
| `frontend/src/api/query-keys.ts` | trades key |
| `frontend/src/routes/watchlist/WatchlistPage.tsx` | 开仓表单 + 加仓/减仓 + 流水 Card |
| `frontend/src/components/a-share/PositionCard.tsx` | PnL 用 avg_cost + 展示均价/股数 |

不动:`closed_positions.jsonl` 形状、postmortem、calibration 反哺三线逻辑、`AddPositionRequest` 旧字段(仅放宽必填)。
