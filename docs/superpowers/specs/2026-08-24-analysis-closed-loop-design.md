# 个股 AI 分析闭环优化设计

## 目标

让 LangGraph 而非模型主动性决定分析何时补证、生成草稿、复核和弃权。证据门控通过时必须进入结构化草稿；真正证据不足时仍安全弃权，并返回可行动的研究报告。

## 状态机

`reason/tools -> assess` 后按确定性规则路由：已有候选或证据门控通过时进入 `draft`；存在关键缺口且预算可用时继续 `research`；预算或模型轮次耗尽且门控仍不通过时进入 `abstain`。`draft` 仅允许调用与持仓状态匹配的收尾工具，失败重试一次；候选通过既有业务校验后进入独立复核。

`analysis_status` 保持 `completed | insufficient_evidence`。失败结果追加 `outcome_reason`、`next_actions` 和 `research_metrics`。`unknowns` 仅保存会影响投资判断的具体未知事实，内部预算代码只进入 `research_metrics.stop_reason`。

## 结果与体验

弃权记录保留已确认 evidence、工具失败和研究指标，不进入回测、校准、候选、通知或交易动作。SSE 的 `status` 事件追加 stage/current/total；CLI 和前端将其显示为业务阶段。弃权卡默认展示中文原因、已确认事实、关键未知、建议动作、失败摘要和研究耗时，原始 trace 继续折叠。

## 兼容与验收

所有新字段均为追加字段，旧 journal 无需迁移。验收要求：内部停止代码不进入 `unknowns`；门控通过后不因研究预算直接弃权；外部服务异常返回可渲染结果；603893.SH 能展示阶段进度，并最终产生经复核结论或具体、可行动的弃权报告。
