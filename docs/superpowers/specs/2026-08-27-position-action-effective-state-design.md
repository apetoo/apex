# 持仓动作有效状态设计

## 背景

000977.SZ 在独立复核通过后，正式报告连续失败：报告写出了当前止损 `71.5`、当前目标 `90` 和既有两档 ladder，但确定性校验收到的候选结果没有 `new_stop`、`new_target`，且 `scale_plan=[]`，因此判定三项与结构化结果不一致。

这不是行情或证据不足。根因是 `position_action` 同时承担了两种互斥语义：

- 作为“本次变更意图”时，字段缺失表示保持现状；
- 作为“变更后的完整快照”时，字段缺失或空数组又表示结果中不存在该内容。

现有提示词要求 `scale_plan` 提交完整 ladder，业务守卫却接受空数组，最终保存又会用空数组覆盖旧计划。报告同时读取结构化候选和历史持仓上下文，因此在“维持原计划”场景中存在两个彼此矛盾的权威来源。继续调整报告格式或正则只能掩盖单个症状。

## 目标

- 明确定义持仓基线、本次变更意图和变更后有效状态三个业务对象。
- 让复核、正式报告、确定性校验和最终保存共享同一份有效状态。
- 消除“字段缺失”“空数组”表示保持、替换还是清空的歧义。
- 保证复核返修后仍能确定性地保留未被修改的持仓参数和未触发 ladder。
- 对旧 journal 和旧持仓数据保持读取兼容。
- 为 000977 的 `revise → pass → report` 路径建立端到端回归保护。

## 非目标

- 不调整行情、基本面、新闻搜索或证据门控策略。
- 不改变 `hold/add/trim/exit` 的交易含义、仓位限制或 ladder 路径模拟规则。
- 不修改前端展示结构。
- 不自动重写已有 journal 记录。
- 不在本次设计中重构整个 `apex/analyze.py`。

## 方案比较

### 方案 A：继续使用补丁语义

所有可选字段都解释为变更：缺失表示保持，空数组表示不修改 ladder。报告前临时把历史值补齐。

优点是工具参数较少；缺点是无法表达“明确清空 ladder”，而且每个消费者都可能重复实现合并逻辑，继续产生不同版本的结果。不采用。

### 方案 B：强制模型每次提交完整快照

要求模型每次重述止损、目标和全部 ladder；任何缺失都拒绝。

优点是下游简单；缺点是让模型负责复制状态，复核返修时容易漏字段或意外删除未触发计划。它把确定性状态管理交给概率模型，不采用。

### 方案 C：变更意图与有效状态分离

模型只表达本次业务意图；程序将其与分析开始时的持仓基线确定性合并，得到完整有效状态。后续消费者只能读取有效状态。

这是采用的方案。它保留补丁式表达的自然性，同时把状态演进、清空语义和最终一致性收回程序控制。

## 领域模型

### PositionBaseline

分析开始时读取的不可变持仓基线，至少包括：

- `stop_loss`
- `target`
- 当前未触发 `scale_plan`
- 持仓股数及其他现有风控上下文

同一次分析从草稿、复核到报告都使用同一个基线快照。分析期间真实持仓被平仓时，沿用现有竞态守卫，禁止保存动作。

### PositionActionProposal

模型提交的本次变更意图：

- `action`: `hold | add | trim | exit`
- 当前动作对应的股数或比例
- 可选 `new_stop`
- 可选 `new_target`
- `ladder_intent`
- `rationale`
- `new_info`

`new_stop` 或 `new_target` 缺失只表示“不修改”，不能表示删除已有止损或目标。

### LadderIntent

ladder 必须显式声明操作，不能再让 `[]` 同时承担多种含义：

- `preserve`：保留基线中全部未触发档位；不得同时提交 ladder 内容。
- `replace`：用提交的完整 ladder 替换基线；允许非空列表。若业务确实需要空计划，应使用 `clear`，不允许 `replace + []`。
- `clear`：明确取消全部未触发档位；不得同时提交 ladder 内容。

默认策略不依赖模型猜测：新结构中 `ladder_intent` 必填。旧调用缺少该字段时只在兼容适配层处理，不能把兼容歧义带入核心模型。

### EffectivePositionPlan

由确定性物化器生成，是变更后唯一权威状态，至少包括：

- `action`
- 当前动作的股数或比例
- `effective_stop`
- `effective_target`
- 完整 `effective_scale_plan`
- `rationale`
- 变更摘要，标明哪些字段保持、替换或清空

物化规则：

- `new_stop` 有值时替换基线止损，否则继承基线止损。
- `new_target` 有值时替换基线目标，否则继承基线目标。
- ladder 按 `preserve / replace / clear` 生成完整有效列表。
- `exit` 是“建议退出”而不是自动成交：有效 ladder 必须为空；在真实退出执行前，基线止损和目标仅作为临时风控值保留供审计，不标记为本次新建议，报告也不要求把它们写成退出后的计划。
- 全部退出档中的无效后续止损仍按既有规范化规则转换为 `None`。

## 数据流与权威边界

```text
PositionBaseline + PositionActionProposal
                    │
                    ▼
        materialize_effective_position_plan
                    │
                    ▼
          EffectivePositionPlan（SSOT）
             ├─ 业务校验 / 路径模拟
             ├─ 独立复核
             ├─ 正式报告提示词
             ├─ 确定性报告校验
             └─ journal / watchlist 保存
```

具体顺序：

1. 分析开始时读取并冻结 `PositionBaseline`。
2. 草稿节点生成 `PositionActionProposal`。
3. 程序物化 `EffectivePositionPlan`，并执行字段、仓位和 ladder 路径校验。
4. 独立复核同时看到基线、变更意图和有效状态，但只评价有效状态是否合理；不能自行合并字段。
5. `revise` 后模型重新提交完整变更意图，程序重新物化；上一版 proposal 不残留。
6. 正式报告只把 `EffectivePositionPlan` 标记为机器结果。历史基线仅可出现在“变化说明”中，不得作为第二套当前计划。
7. 确定性报告校验只与同一份有效状态比较。
8. 保存阶段直接使用已复核、已报告的有效状态，不再重新解释 proposal。

## 报告语义

持仓报告需要区分“当前有效值”和“本次变化”：

- `**当前有效止损：71.5**`
- `**当前有效目标：90**`
- 若本次未调整，可写“本次维持”；但数值来自有效状态。
- 条件触发计划必须完整列出 `effective_scale_plan`。
- `clear` 时明确写“已取消全部未触发计划”，且不得列出历史档位。
- 当前动作与未来条件动作继续分离。

报告不再使用 `new_stop/new_target` 标签表达最终状态，因为“new”只适合变更意图，无法覆盖“维持原值”。兼容旧报告解析只用于读取历史记录，不进入新报告生成链路。

## 兼容策略

### 旧模型/旧调用适配

在工具契约切换期间，旧 payload 通过单一适配器转换：

- 缺少 `scale_plan`：解释为 `preserve`。
- 提供非空 `scale_plan`：解释为 `replace`。
- 提供空 `scale_plan=[]`：不能可靠区分 preserve 与 clear。为避免意外删除计划，统一解释为 `preserve`，并记录兼容警告；清空只能通过新字段显式表达。

适配发生一次后，核心流程只接受新领域对象。

### 旧存储

- 旧 position action journal 继续按原结构读取。
- 当前 watchlist 中的 `plan.scale_plan` 直接成为 baseline。
- 新 journal 可以追加 proposal/effective 元数据，但保持现有 `position_action` 结果字段供前端和统计代码读取。

## 错误处理

- `ladder_intent` 缺失或与 ladder 内容冲突：草稿校验失败，要求模型重新提交，不进入复核。
- 物化后的 ladder 不满足路径或风控规则：返回具体业务原因，拒绝候选。
- 复核返修后 proposal 无效：回到草稿，不得使用上一次有效状态兜底。
- baseline 与保存时真实持仓发生不可兼容变化或持仓已关闭：中止保存并返回明确竞态原因。
- 报告与有效状态不一致：允许定向重试一次；仍失败则 `report_validation_failed`，但错误摘要必须同时带 effective 值，避免再次出现含义不明的 `expected=[]`。

## 测试策略

### 领域物化测试

- 缺少 `new_stop/new_target` 时继承 baseline。
- 提供新值时替换 baseline。
- `preserve` 保留全部未触发 ladder。
- `replace` 使用完整新 ladder。
- `clear` 明确清空 ladder。
- 非法 intent 组合确定性拒绝。
- `exit` 生成无 ladder 的退出有效状态。

### 状态机回归测试

- 复现最新 000977：第一次 proposal 被 `revise`，第二次 proposal 表达维持原止损、目标和 ladder，复核通过；报告列出 `71.5`、`90` 和两档历史计划并成功完成。
- `revise → draft` 后不残留上一版 proposal，但始终使用相同 baseline 重新物化。
- 报告提示词、校验器和保存函数收到同一个有效状态对象。
- `clear` 不会被误解释为 preserve，preserve 不会清空 watchlist。

### 兼容与回归测试

- 旧空 `scale_plan` payload 安全适配为 preserve。
- 旧非空 payload 适配为 replace。
- 现有 position action、adherence、monitor 和 watchlist 测试保持通过。
- 完整 Python 测试通过；现有与本功能无关的已知失败单独记录，不降低断言。

## 成功标准

- 最新 000977 场景不再产生止损、目标或 ladder 的结构化不一致。
- 同一分析中不存在两份可被下游当成当前计划的权威数据。
- `scale_plan=[]` 不再能意外清空既有计划。
- 每次保存的 watchlist 状态与正式报告展示状态完全一致。
- 任何消费者无需自行猜测字段缺失或空数组的业务含义。

## 实施边界

实现应围绕一个纯物化器和明确的领域对象展开，避免继续在报告校验器、提示词、保存函数中分别增加补丁。必要修改预计覆盖：工具 schema、草稿校验、复核上下文、报告上下文与校验、position action 最终保存，以及对应测试；不扩展到其他分析或前端功能。
