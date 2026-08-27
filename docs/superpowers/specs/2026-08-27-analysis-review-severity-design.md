# 分析独立复核分级设计

## 背景

个股分析候选结论通过证据收敛后，会交给独立复核模型检查。当前流程直接信任模型返回的 `abstain`：即使 issues 只描述盘中/收盘口径、资金表述或交易计划调整等轻微问题，也会结束分析并落盘为 `insufficient_evidence`。

000977.SZ 的最新分析触发了该问题：证据已经收敛、没有未知项、候选动作是 `hold`，但复核器将可修订的表达问题和止损上移误判为需要弃权。

## 目标

- 只有重大事实冲突、影响结论方向的无证据主张或关键未知才能阻断分析。
- 轻微表达、时间口径和可解释的交易计划调整不能直接导致 `review_failure`。
- 保留一次候选修订机会；重大冲突修订后仍存在时继续安全弃权。
- 区分历史交易计划基线与本次候选提出的新计划。

## 非目标

- 不改变证据收集预算、研究收敛规则或正式报告校验。
- 不自动采纳缺少理由的止损、目标价或加仓计划修改。
- 不降低重大事实冲突的安全门槛。

## 方案

### 结构化复核结果

复核器输出：

```json
{
  "outcome": "pass|revise|abstain",
  "issues": [
    {
      "message": "问题说明",
      "severity": "minor|material",
      "blocking": false
    }
  ]
}
```

解析层兼容旧的字符串 issues，避免模型偶发返回旧格式或已有测试失效。旧格式按保守规则分类：明确包含重大冲突、关键未知、事实错误或无支撑方向性主张时视为 material；其余视为 minor。

### 确定性状态转换

- 没有 blocking/material issue：`pass`。
- 仅有 minor issue：第一次复核转为 `revise`；修订后仅剩 minor 则 `pass`。
- 存在 material issue：第一次转为 `revise`；修订后仍存在 material issue 则 `abstain`，记录 `review_failure`。
- 复核服务失败、JSON 连续解析失败等技术故障仍直接 `abstain`。
- 模型给出的 outcome 不能绕过上述确定性规则。

### 交易计划语义

复核提示明确：`system_context` 中已有 ladder/止损是历史基线；候选中的 `new_stop`、`new_target`、`scale_plan` 是本次拟议计划。两者数值不同不是天然冲突。只有候选未给调整依据、违反持仓风险约束或候选内部字段互相矛盾时，才记录问题。

盘中站上均线与“尚未收盘站稳”可以同时成立，但候选必须明确时间口径。未写清时属于 minor 表达问题，不属于证据不足。

## 测试

- 复现 000977：首次复核指出 minor 问题，修订后仍返回 minor/`abstain`，最终应完成而不是 `review_failure`。
- 旧 ladder 与有理由的 `new_stop` 不作为 material issue。
- 首次 material issue 触发一次修订。
- 修订后仍有 material issue 时保持 `review_failure`。
- 复核服务或 JSON 解析连续失败时仍保持 `review_failure`。
- 运行完整 Python 测试，并确认没有改变正式报告一致性门控。

## 兼容性与风险

主要风险是把真实冲突误降级。通过保守识别 material issue、保留二次重大冲突弃权，以及不改变报告最终校验来控制风险。journal 对外结构保持不变；结构化 issue 仅用于复核内部决策，落盘失败信息继续使用字符串，避免前端兼容性变化。
