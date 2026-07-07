# 持仓推送接口文档 (Apex Position Push API)

> 版本：`1.0.0`  ·  最后更新：2026-07-04
> 适用项目：apex（A 股个人交易回路）

## 1. 概述

apex 作为**生产方**，在持仓状态变化时主动调用**消费方**（合作伙伴系统）提供的 HTTP webhook，把持仓信息推送出去。

- **方向**：apex → 消费方（出站 HTTP POST，**不是**消费方来拉）。
- **协议**：HTTPS（生产）/ HTTP（本地联调）。JSON，UTF-8，`Content-Type: application/json`。
- **两种推送模式**：
  - **全量推送 (`full`)**：当前所有活跃持仓的快照。用于首次接入、定期对账、故障恢复。
  - **增量推送 (`incremental`)**：单条持仓变更事件。状态一变即推，准实时。
- **单端点**：两种模式都打到同一个 URL，用 envelope 里的 `push_type` 区分。消费方只注册一个地址。
- **货币**：CNY（人民币）。A 股约定：红涨绿跌，最小交易单位 100 股/手。
- **时区**：所有时间戳为带时区的 ISO8601， Asia/Shanghai (`+08:00`)，如 `2026-07-04T14:30:00+08:00`。

## 2. 接入配置

apex 侧在 `config.yaml` 新增 `push` 段：

```yaml
push:
  enabled: true                                      # 总开关；false 时全量/增量都不发
  base_url: "https://partner.example.com/api/apex"   # 消费方 webhook base，不带路径
  path: "/positions/push"                            # 拼到 base_url 后面；默认 /positions/push
  timeout_seconds: 10
  retry:
    max_attempts: 5                                  # 含首次，共 5 次
    base_seconds: 1                                  # 指数退避基数
    max_seconds: 60
  full:
    cron: "0 16 * * 1-5"                             # 每个交易日 16:00 收盘后全量对账
    on_startup: true                                 # apex 启动时先推一次全量
  incremental:
    enabled: true                                    # 持仓变更实时推
  log_file: "~/.stock-journal/push_log.jsonl"        # 推送审计日志（落盘，供 replay/排障）
```

## 3. 鉴权

本接口**不设应用层鉴权** —— 不带 token、不带签名，请求体明文 JSON。

若需接入控制，由消费方在网关层自行实现（如 nginx IP 白名单、内网隔离、mTLS）。`push_id` / `seq` 仍可用于业务层去重与缺口检测，但**不**作为身份凭证。

## 4. 统一信封 (Envelope)

所有推送共用一个信封，消费方先解信封再按 `push_type` 分发：

```jsonc
{
  "push_type": "full" | "incremental",
  "push_id": "01HXYZABCDEFGHJKMNPQRSTVWXYZ",      // ULID，幂等键，消费方据此去重
  "pushed_at": "2026-07-04T14:30:00+08:00",       // apex 发出时刻
  "source": "apex",
  "source_version": "1.0.0",                        // apex 版本，便于消费方兼容
  "seq": 142,                                       // 单调递增序列号（见 §9.3）
  "data": { ... }                                   // 形状随 push_type 变，见 §5 / §6
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `push_type` | string | `full` / `incremental` |
| `push_id` | string | 全局唯一，ULID。**同一笔推送因重试会发多次，`push_id` 相同** —— 消费方必须幂等。 |
| `pushed_at` | string | ISO8601 带时区 |
| `source` | string | 固定 `apex` |
| `source_version` | string | 信封/数据 schema 版本，当前 `1.0.0` |
| `seq` | int | apex 侧持久化的单调递增计数器，**增量必填**；全量也带（取当前最大值），便于消费方对齐水位 |
| `data` | object | 见下两节 |

## 5. 全量推送 (`push_type: "full"`)

发送当前 `active_positions` 全量快照。**幂等语义：last-write-wins** —— 消费方收到后应整体覆盖该账户下的持仓集合（以 `ts_code` 为 key）。

### 5.1 `data` 结构

```jsonc
{
  "snapshot_at": "2026-07-04T15:05:00+08:00",
  "account": { Account },                          // 见 §7.3，可为 null
  "positions": [ Position, ... ],                  // 见 §7.1，空持仓发空数组
  "position_count": 2,
  "total_market_value": 12345.67,                  // 实时市值合计；拉不到行情则为 null
  "total_cost": 11800.00,                          // sum(avg_cost * shares)
  "total_unrealized_pnl": 545.67                   // = total_market_value - total_cost；行情缺则 null
}
```

### 5.2 触发时机

- **定时**：`push.full.cron`（默认每交易日 16:00）。
- **启动**：`push.full.on_startup=true` 时，apex 进程启动推一次。
- **手动**：apex 暴露 `POST /api/push/full` 供人工触发（排障 / 重新对账）。
- **故障兜底**：增量推送连续重试耗尽后，下一次全量推送会自动对账（见 §9）。

## 6. 增量推送 (`push_type: "incremental"`)

持仓状态一变即推一条事件。**幂等语义：按 `event_id` 去重；按 `seq` 检测缺口**。

### 6.1 `data` 结构

```jsonc
{
  "event_type": "position_opened",                 // 见 §6.2 事件总表
  "event_id": "20260704T143000-0001",              // 优先复用 trade_id；非 trade 事件用 ULID
  "event_at": "2026-07-04T14:30:00+08:00",         // 事件实际发生时刻（= trade.traded_at 或操作时刻）
  "ts_code": "603019.SH",
  "name": "中科曙光",
  "before": { Position } | null,                   // 变更前快照；opened 事件为 null
  "after":  { Position } | null,                   // 变更后快照；closed/archived 为 null
  "trade":  { Trade } | null,                      // 由买卖触发时带；advice_updated 等非 trade 事件为 null
  "close":  { Close } | null                       // 仅 position_closed 带平仓明细
}
```

### 6.2 事件类型总表

| `event_type` | 触发的 apex 入口 | `before` | `after` | `trade` | `close` | 语义 |
|---|---|---|---|---|---|---|
| `position_opened` | `add_position` / `buy`(开仓) / `promote_candidate` | null | 新持仓 | buy | null | 新建一笔活跃持仓 |
| `position_increased` | `buy`(加仓) | 旧 | 更新后 | buy | null | 加仓：shares↑、avg_cost 重算 |
| `position_decreased` | `sell`(减仓) | 旧 | 更新后 | sell | null | 减仓：shares↓、部分实现盈亏 |
| `position_closed` | `sell`(卖光) / `close_position` | 平仓前 | null | sell | Close | 平仓：移出活跃、写 closed_positions |
| `position_advice_updated` | `update_advice` | 旧 | 更新后 | null | null | 仅止损/目标/校准确信度变更 |
| `position_replaced` | `replace_position` | 旧持仓 | 新持仓 | null | null | 替换：旧归档 + 新建仓 |
| `position_archived` | `archive_entry`(对 active_positions) | 归档前 | null | null | null | 手动软删除，非平仓 |

> **候选 (candidate) 不推**。本接口只推「活跃持仓」生命周期。候选 → 持仓的晋升以 `position_opened` 体现（`trade` 可能为 null，因晋升不一定经过 buy trade）。

### 6.3 各事件示例

**(a) `position_opened`** — 新开仓（买入触发）

```jsonc
{
  "push_type": "incremental",
  "push_id": "01HXYZ...",
  "pushed_at": "2026-07-04T14:30:02+08:00",
  "source": "apex",
  "source_version": "1.0.0",
  "seq": 143,
  "data": {
    "event_type": "position_opened",
    "event_id": "20260704T143000-0001",
    "event_at": "2026-07-04T14:30:00+08:00",
    "ts_code": "603019.SH",
    "name": "中科曙光",
    "before": null,
    "after": {
      "ts_code": "603019.SH", "name": "中科曙光",
      "entry_date": "2026-07-04", "entry_price": 42.50, "avg_cost": 42.50,
      "shares": 200, "lots": 2,
      "stop_loss": 40.00, "target": 50.00,
      "risk_amount": 500.00, "calibrated_confidence": 0.72,
      "strategy": "momentum", "regime_at_open": "bull",
      "expires_at": "2026-07-14", "status": "active"
    },
    "trade": {
      "trade_id": "20260704T143000-0001", "side": "buy",
      "fill_price": 42.50, "shares": 200, "amount": 8500.00,
      "realized_pnl": null, "realized_pnl_pct": null,
      "avg_cost_after": 42.50, "shares_after": 200,
      "strategy": "momentum", "regime": "bull",
      "exit_reason": null, "note": "", "traded_at": "2026-07-04T14:30:00+08:00"
    },
    "close": null
  }
}
```

**(b) `position_increased`** — 加仓

```jsonc
"data": {
  "event_type": "position_increased",
  "event_id": "20260704T150000-0002",
  "event_at": "2026-07-04T15:00:00+08:00",
  "ts_code": "603019.SH", "name": "中科曙光",
  "before": { "ts_code": "603019.SH", "shares": 200, "avg_cost": 42.50, "...": "..." },
  "after":  { "ts_code": "603019.SH", "shares": 400, "avg_cost": 43.10, "entry_price": 42.50, "...": "..." },
  "trade":  { "trade_id": "20260704T150000-0002", "side": "buy", "fill_price": 43.70, "shares": 200, "amount": 8740.00, "avg_cost_after": 43.10, "shares_after": 400, "traded_at": "2026-07-04T15:00:00+08:00" },
  "close": null
}
```

**(c) `position_closed`** — 卖光平仓

```jsonc
"data": {
  "event_type": "position_closed",
  "event_id": "20260704T160000-0003",
  "event_at": "2026-07-04T16:00:00+08:00",
  "ts_code": "603019.SH", "name": "中科曙光",
  "before": { "ts_code": "603019.SH", "shares": 400, "avg_cost": 43.10, "...": "..." },
  "after": null,
  "trade": { "trade_id": "20260704T160000-0003", "side": "sell", "fill_price": 46.20, "shares": 400, "amount": 18480.00, "realized_pnl": 1240.00, "realized_pnl_pct": 0.0719, "avg_cost_after": null, "shares_after": 0, "exit_reason": "target_hit", "traded_at": "2026-07-04T16:00:00+08:00" },
  "close": {
    "exit_date": "2026-07-04", "actual_exit_price": 46.20,
    "exit_reason": "target_hit", "days_held": 0, "trading_days_held": 0,
    "high_during_hold": 46.50, "low_during_hold": 42.30,
    "realized_pnl_pct": 0.0719, "realized_pnl_amount": 1240.00,
    "closed_at": "2026-07-04T16:00:05+08:00"
  }
}
```

**(d) `position_advice_updated`** — 仅改止损/目标（非 trade）

```jsonc
"data": {
  "event_type": "position_advice_updated",
  "event_id": "01HXYZADVICE0001",            // 非 trade 事件用 ULID
  "event_at": "2026-07-04T14:35:00+08:00",
  "ts_code": "603019.SH", "name": "中科曙光",
  "before": { "ts_code": "603019.SH", "stop_loss": 40.00, "target": 50.00, "calibrated_confidence": 0.72, "...": "..." },
  "after":  { "ts_code": "603019.SH", "stop_loss": 41.50, "target": 52.00, "calibrated_confidence": 0.78, "...": "..." },
  "trade": null,
  "close": null
}
```

## 7. 数据模型

### 7.1 `Position` — 活跃持仓快照

对外字段名做了清晰化（`position_size_shares` → `shares`），内部映射见 §13。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ts_code` | string | ✓ | 带交易所后缀：`603019.SH` / `002050.SZ` / `838810.BJ` |
| `name` | string | ✓ | 证券名称 |
| `entry_date` | string | ✓ | 首次入场日期 `YYYY-MM-DD` |
| `entry_price` | number | ✓ | 首次入场价（不变） |
| `avg_cost` | number | ✓ | 加权平均成本（加仓后重算） |
| `shares` | int | ✓ | 持有股数 |
| `lots` | int | ✓ | 手数 = `shares / 100`（A 股 100 股/手） |
| `stop_loss` | number \| null | | 止损价 |
| `target` | number \| null | | 目标价 |
| `risk_amount` | number \| null | | 单笔风险金额（CNY） |
| `calibrated_confidence` | number \| null | | 校准后置信度 `0–1` |
| `strategy` | string \| null | | 策略归属：screener 策略名 / `manual` / `analyze` |
| `regime_at_open` | string \| null | | 开仓时市场状态标签 |
| `expires_at` | string \| null | | 持仓过期日 `YYYY-MM-DD`（apex 内部用于触发监控） |
| `status` | string | ✓ | 快照里固定 `active` |

### 7.2 `Trade` — 交易留痕（增量事件中 `trade` 字段）

对齐 `apex/trades.py` 的 `append_trade` 返回结构。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `trade_id` | string | ✓ | `YYYYMMDDTHHMMSS-NNNN`，同秒多笔递增 |
| `side` | string | ✓ | `buy` / `sell` |
| `fill_price` | number | ✓ | 成交价 |
| `shares` | int | ✓ | 成交股数 |
| `amount` | number | ✓ | `fill_price * shares` |
| `realized_pnl` | number \| null | | 已实现盈亏（CNY）；buy 为 null |
| `realized_pnl_pct` | number \| null | | 已实现盈亏比例（小数，如 `0.0719` 表 7.19%） |
| `avg_cost_after` | number \| null | | 该笔之后的持仓均价；卖光为 null |
| `shares_after` | int \| null | | 该笔之后持仓股数；卖光为 0 |
| `strategy` | string \| null | | 策略归属 |
| `regime` | string \| null | | 成交时市场状态 |
| `exit_reason` | string \| null | | 仅 `sell` 带值。枚举：`stop_hit` / `target_hit` / `manual` / `expired` / `other` |
| `note` | string | ✓ | 备注，可空串 |
| `traded_at` | string | ✓ | ISO8601 带时区 |

### 7.3 `Account` — 账户级配置

对齐 `apex/account.py` 的 `account.json`。

```jsonc
{
  "total_capital": 100000.00,        // 总资金（CNY）
  "risk_per_trade_pct": 1.0,         // 单笔风险比例 %
  "max_total_risk_pct": 10.0,        // 总风险上限 %
  "currency": "CNY",
  "updated_at": "2026-07-01T09:00:00+08:00"
}
```

### 7.4 `Close` — 平仓明细（仅 `position_closed`）

对齐 `closed_positions.jsonl` 的 `close` 段。

| 字段 | 类型 | 说明 |
|---|---|---|
| `exit_date` | string | 平仓日期 |
| `actual_exit_price` | number | 实际平仓价 |
| `exit_reason` | string | 同 §7.2 枚举 |
| `days_held` | int | 自然日持有天数 |
| `trading_days_held` | int \| null | 交易日持有天数 |
| `high_during_hold` | number \| null | 持有期间最高价 |
| `low_during_hold` | number \| null | 持有期间最低价 |
| `realized_pnl_pct` | number \| null | 同 §7.2 |
| `realized_pnl_amount` | number \| null | 已实现盈亏金额（CNY） |
| `closed_at` | string | 平仓记录写入时刻 ISO8601 |

## 8. 触发时机（apex mutation → 事件映射）

实现时在 `apex/watchlist.py` 的以下入口埋推送钩子（建议统一收口到一个 `apex/push.py` 的 `notify(event_type, before, after, trade, close)` 函数，由 watchlist 各函数 return 前调用）：

| apex 入口 | 文件:函数 | 触发事件 |
|---|---|---|
| 新增持仓 | `watchlist.add_position` | `position_opened` |
| 买入（开仓） | `watchlist.buy`（existing is None 分支） | `position_opened` + trade=buy |
| 买入（加仓） | `watchlist.buy`（existing 分支） | `position_increased` + trade=buy |
| 卖出（减仓） | `watchlist.sell`（shares < holding） | `position_decreased` + trade=sell |
| 卖出（卖光） | `watchlist.sell`（shares ≥ holding） | `position_closed` + trade=sell + close |
| 平仓 | `watchlist.close_position` | `position_closed` + close |
| 更新 advice | `watchlist.update_advice` | `position_advice_updated` |
| 替换持仓 | `watchlist.replace_position` | `position_replaced` |
| 归档持仓 | `watchlist.archive_entry`（section=active_positions） | `position_archived` |
| 候选晋升 | `watchlist.promote_candidate` | `position_opened`（trade=null） |

> `sell()` 卖光分支内部已调 `close_position(record_trade=False)`，**只在 `sell()` 出口推一次 `position_closed`**，避免双推。实现时让 `close_position` 的推送由 `record_trade` 语义控制，或 `sell()` 卖光分支直接接管推送、`close_position` 仅在被直接调用（`POST /close`）时推。

## 9. 投递语义

### 9.1 重试

- 首次发送失败（连接超时 / 5xx / 网络错误）→ 指数退避重试：`1s, 2s, 4s, 8s, 16s`，最多 `retry.max_attempts` 次。
- **4xx 不重试**（除 408/429）：消费方明确拒绝，重试无意义，记日志后放弃。
- 429（限流）按 `Retry-After` 头等待，无则按退避重试。

### 9.2 幂等

- **`push_id` 是幂等键**。同一笔推送因重试会发多次，消费方必须按 `push_id` 去重。
- 增量事件另带 `event_id`（同 `push_id` 语义，但更贴近业务：trade 事件 = `trade_id`）。消费方可任选其一去重。
- 全量推送幂等 = 按 `snapshot_at` + `push_id`，覆盖写。

### 9.3 顺序与缺口检测

- `seq` 是 apex 侧持久化在 `~/.stock-journal/push_seq.json` 的单调递增整数，**每发一条（无论 full/incremental）+1**。
- 消费方记录已收到的最大 `seq`。若新收到的 `seq > last_seen + 1`，说明中间有推送丢失 → **触发一次全量对账**（消费方可调 apex 的 `POST /api/push/full`，或等下一个 cron 全量）。
- `seq` 不保证跨进程重启绝对连续（崩溃恢复时可能跳号），消费方应把它当作「缺口检测线索」而非「严格连续序号」。

### 9.4 失败兜底

- 增量推送重试耗尽 → 写 `push_log.jsonl` 标记 `failed`，**不阻断** apex 主流程（持仓变更已落盘，推送是旁路）。
- 下一次全量推送（cron 或手动）会自然对账，把丢失的增量以快照差异的形式补上。

## 10. 响应契约

消费方收到推送后应在 `timeout_seconds`（默认 10s）内响应：

### 成功

```
HTTP 200 / 201 / 202
Content-Type: application/json

{ "accepted": true, "push_id": "01HXYZ..." }
```

- `202 Accepted` 最贴切（异步处理）。
- 消费方只要回了 2xx，apex 就认为投递成功、不再重试。

### 失败

| 状态码 | apex 行为 |
|---|---|
| 2xx | 成功，停止重试 |
| 400 / 401 / 403 / 410 / 422 | 永久失败，**不重试**，记日志 |
| 408 / 429 / 5xx | 瞬时失败，按退避重试 |
| 超时 / 连接错误 | 瞬时失败，按退避重试 |

### 响应体（失败时可选）

```jsonc
{ "accepted": false, "error": "invalid payload", "retryable": false }
```

`retryable` 字段供消费方显式告知 apex 是否该重试，覆盖状态码默认策略。

## 11. 推送日志

apex 侧每次推送（含每次重试）追加一行到 `~/.stock-journal/push_log.jsonl`，用于排障与 replay：

```jsonc
{
  "push_id": "01HXYZ...",
  "seq": 143,
  "push_type": "incremental",
  "event_type": "position_opened",            // full 时为 null
  "ts_code": "603019.SH",
  "attempt": 1,                                // 第几次尝试（1 起）
  "status": "success" | "failed" | "timeout",
  "http_status": 202,                          // 失败时为 null
  "error": null,                               // 失败原因
  "latency_ms": 87,
  "payload_ref": null,                         // 可选：payload 太大时存到 push_payloads/<push_id>.json 的路径
  "sent_at": "2026-07-04T14:30:02+08:00"
}
```

> payload 较小（< 64KB）时直接在 `push_log` 留 `payload` 字段；超过则落单独文件、`payload_ref` 指向路径。**增量事件 payload 一律小，直接内联**；全量可能较大（持仓多时），走 `payload_ref`。

## 12. apex 侧管理端点（供消费方/运维触发）

这些是 apex 自己暴露的 HTTP 端点（`/api/push/*`），不是消费方实现的：

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/push/full` | 手动触发一次全量推送 |
| `POST` | `/api/push/replay?since=<seq>` | 重放 `seq > since` 的所有推送（从 push_log 读） |
| `GET` | `/api/push/status` | 返回 `{ last_seq, last_push_at, last_status, pending_failures }` |
| `GET` | `/api/push/log?limit=100` | 读最近 N 条推送日志 |

> 消费方检测到 `seq` 缺口时，可调 `POST /api/push/replay?since=<last_seen>` 让 apex 补推，比等 cron 全量更及时。

## 13. 字段与内部存储映射

实现 `apex/push.py` 时按下表把内部字段翻译成对外字段：

| 对外字段 | 内部来源 |
|---|---|
| `Position.ts_code` | `active_positions[].ts_code`（已 normalize，带后缀） |
| `Position.shares` | `active_positions[].position_size_shares` |
| `Position.lots` | `shares / 100`（运行时算） |
| `Position.entry_price` | `active_positions[].entry_price` |
| `Position.avg_cost` | `active_positions[].avg_cost`（缺则回落 `entry_price`） |
| `Position.stop_loss` / `target` | 同名字段 |
| `Position.risk_amount` | `active_positions[].risk_amount` |
| `Position.calibrated_confidence` | `active_positions[].calibrated_confidence` |
| `Position.strategy` / `regime_at_open` | 同名字段 |
| `Position.expires_at` / `status` | 同名字段 |
| `Trade.*` | `trades.jsonl` 行（`apex/trades.py:append_trade` 返回） |
| `Account.*` | `account.json`（`apex/account.py:load`） |
| `Close.*` | `closed_positions.jsonl` 的 `close` 段 |

## 14. 版本与演进

- `source_version` 跟随信封 schema 版本。当前 `1.0.0`。
- **向后兼容演进规则**：只加字段、不删字段、不改字段类型、不改枚举值语义。新增事件类型不算破坏性变更（消费方应忽略未知 `event_type`）。
- 破坏性变更 → 升 `source_version` 主版本号，消费方按版本路由。

---

## 附：全量推送完整示例

```jsonc
POST https://partner.example.com/api/apex/positions/push
Content-Type: application/json

{
  "push_type": "full",
  "push_id": "01HXYZFULL0001",
  "pushed_at": "2026-07-04T16:00:00+08:00",
  "source": "apex",
  "source_version": "1.0.0",
  "seq": 200,
  "data": {
    "snapshot_at": "2026-07-04T16:00:00+08:00",
    "account": {
      "total_capital": 100000.00,
      "risk_per_trade_pct": 1.0,
      "max_total_risk_pct": 10.0,
      "currency": "CNY",
      "updated_at": "2026-07-01T09:00:00+08:00"
    },
    "positions": [
      {
        "ts_code": "603019.SH", "name": "中科曙光",
        "entry_date": "2026-06-20", "entry_price": 42.50, "avg_cost": 42.80,
        "shares": 200, "lots": 2,
        "stop_loss": 40.00, "target": 50.00,
        "risk_amount": 560.00, "calibrated_confidence": 0.72,
        "strategy": "momentum", "regime_at_open": "bull",
        "expires_at": "2026-07-04", "status": "active"
      }
    ],
    "position_count": 1,
    "total_market_value": 9300.00,
    "total_cost": 8560.00,
    "total_unrealized_pnl": 740.00
  }
}
```

响应：

```jsonc
HTTP 202
{ "accepted": true, "push_id": "01HXYZFULL0001" }
```
