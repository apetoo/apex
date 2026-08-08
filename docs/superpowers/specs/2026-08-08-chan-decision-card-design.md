# 缠论决策卡设计

## 目标

在不引入多周期聚合、全市场扫描和信号回测的前提下，把现有单周期缠论结构转化为可解释、可确认、可失效并能安全加入候选的决策卡。

成功标准：用户能在缠论页直接看懂当前结构处于观察、等待确认、已确认或已失效中的哪一种状态，看到机械确认价与失效价，并经确认弹窗把有效做多结构写入现有候选系统。

## 判定模型

现有 czsc 笔、中枢和 BSP 结果保持不变。在 `get_structure` 的输出阶段增加纯函数式决策层，返回 `decision`：

- `bias`: `long | neutral | risk`
- `setup`: `bsp_buy | zs_breakout | none`
- `state`: `watching | pending | confirmed | invalid`
- `signal_dt`、`bars_since_signal`
- `confirm_price`、`invalidation_price`
- `trigger_price`、`trigger_low`、`trigger_high`
- `candidate_eligible`、`ineligible_reason`
- `basis`: 供界面展示的简短依据列表

决策优先级固定为：最近 10 根已完成 K 线内的有效买点，其次是已确认中枢向上突破，再其次是卖点或向下跌破风险，最后是普通观察。

### 买点规则

- 只考虑最近一个一买、二买或三买。
- 确认价固定为买点端点所在 K 线的最高价；失效价固定为 BSP 买点价格。
- 从信号后的已完成 K 线按时间顺序检查收盘价。最近状态以结构是否曾确认及当前是否已跌破失效价为准：当前收盘跌破失效价为 `invalid`；否则，只要任一后续收盘站上确认价即为 `confirmed`；尚未站上为 `pending`。
- 距当前超过 10 根已完成 K 线时保留展示，但 `candidate_eligible=false`，原因是信号过期。
- `pending` 与未过期的 `confirmed` 可以生成候选；`invalid` 不允许。
- 候选使用 `trigger_direction="above"`，`trigger_price=confirm_price`，`stop_advice=invalidation_price`。

### 中枢突破规则

- 使用现有除权过滤口径下的最近已确认中枢。
- 当前已完成 K 线收盘高于中枢上沿 `zg` 时，状态为 `confirmed`、方向为 `long`。
- 候选不追当前价，等待回踩中枢上沿：`trigger_low=zg`，`trigger_high=zg×1.01`，兼容字段 `trigger_price=zg`，`stop_advice=zd`。
- 当前收盘低于 `zd` 时作为 `risk` 展示，不允许生成做多候选。

卖点与向下跌破只产生风险提示。无买点、无中枢、数据不足或无法得到可靠阈值时返回 `watching`，不报错且不允许生成候选。所有判断只使用已完成 K 线。

## 页面与候选闭环

“结构摘要”升级为“缠论决策卡”，显示结论、状态、确认价、失效价、信号距今 K 线数和两至三条判定依据。等待确认使用中性色，已确认使用多头色，已失效与风险提示使用风险色；只有 `candidate_eligible=true` 时启用“加入候选”。

点击“加入候选”在当前页打开确认弹窗，预填代码、结构类型、触发价或回踩区间、结构失效价、周期、信号时间和说明；字段允许人工修改。目标价保持为空，由后续 AI 分析或人工补充。提交复用现有候选 mutation：

- `strategy="chan"`
- `setup` 写入具体买点类型或“中枢突破回踩”
- `note` 记录周期、信号时间及确认/失效规则
- 同代码已有候选时，提交前提示将按现有 upsert 语义覆盖原候选参数

成功后关闭弹窗并显示成功反馈；失败时保留用户输入和错误信息。“发起 AI 分析”入口保留，继续自动带入股票代码，本次不修改 AI Prompt，也不自动调用 AI。

## 测试与边界

后端单元测试覆盖买点等待确认、确认、确认后失效、超过 10 根、突破回踩参数、卖点风险、无结构、除权过滤后无可用中枢和数据不足。接口契约测试确认新增字段可序列化，旧字段不变。

前端测试覆盖四种状态、按钮可用性、弹窗预填与修改、买点单价触发、中枢回踩带、失败保留、成功反馈和既有候选覆盖提示。最终执行完整 Python 测试、前端 Vitest、TypeScript/Vite build 与 `git diff --check`。

## 非目标

本版本不实现多周期联立、全市场缠论扫描、背驰证据、目标价投射、逐 K 线信号回测或自动交易。
