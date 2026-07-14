---
name: growth-stock
description: 成长股分析方法论。成长股=科技/医药/新能源，circ_mv 100-500亿，PE偏高。估值看 PS/PEG，禁静态 PE 偏空。
applies_to: [analyze, chat]
source:
  - apex/prompts/expert-persona.md (类型表 + 6a 财务深度)
  - apex/analyze.py record_verdict 校验 (静态 PE 偏空硬门控)
---

# 成长股分析方法论

> 成长股的估值核心是「未来增长能否消化当前估值」，不是静态 PE 高低。
> 这是本类股票最容易出错的地方，也是硬门控盯死的点。

## 1. 识别特征

| 维度 | 特征 |
|------|------|
| 流通市值 | circ_mv 100-500 亿 |
| PE | 偏高（>30 或无利润） |
| 行业 | 科技 / 医药 / 新能源 |

无法明确归类或多特征混合 -> 归「均衡型」，不硬塞成长股。

## 2. 四维权重（替代等权）

技术 30% / 基本 40% / 资金 15% / 情绪 15%

分析重点：**营收增速、渗透率、研发投入**。估值看 **PS / PEG**，不看静态 PE。

## 3. 财务深度（必须引用，不得自算）

`get_fundamentals` 返回 `valuation` + `quarters` + `summary`，必须引用：

- 营收增速 `or_yoy` 是否 > 20%
- 毛利率趋势（扩张还是收缩）
- ROE 改善速度
- `fcff` 是否为正（能否自我造血）
- `summary.flags` 中的 Python 预计算风险标记 -> 直接引用，不要自己重算 ROE 趋势或负债率阈值

## 4. 估值规则：禁静态 PE 偏空（核心）

成长股估值核心 = 「未来增长能否消化当前估值」。**静态 PE_TTM 不得单独作为偏空结论的主要依据。**

估值分析必须优先采用：Future EPS / Forward PE / PEG / 利润增速 / 行业增速。
- 前瞻数据：`mx_data_query` 查一致预期 / 业绩预告
- `get_fundamentals` 已给 `trailing PEG = pe_ttm / 净利润同比增速` 供参考

**判定树：**
```
成长股 -> 利润未来三年是否高速增长？
  是 -> Forward PE 是否快速下降？
    是 -> PE 高不是问题（高 PE 合理）
  否 -> PEG 是否 > 2？
    是 -> 估值开始危险
```

即：不是「PE=190 -> 危险」，而是「PE=190 -> 利润未来还能翻倍吗？-> 能 -> PE 不是核心问题」。

**只有三者同时成立** -- 利润增长明显放缓 + Forward PE 仍极高 + PEG 明显失衡 -- 才能把估值作为主要空头证据。否则高 PE 只能作为**风险提示**写入 evidence，不得据此偏空。

### 4a. 硬门控（系统强制，analyze 侧）

`record_verdict` 校验：**成长股 + 偏空 + `valuation_basis` ∈ {None, `static_pe_only`} -> 拒绝**，逼补前瞻估值后重调。

`valuation_basis` 枚举（偏空类必填）：
- `forward_valuation` = 基于 Forward PE / PEG / 一致预期等前瞻估值（**成长股偏空唯一允许的估值依据**）
- `static_pe_only` = 仅静态 PE_TTM（成长股偏空会被拒）
- `non_valuation` = 估值非主要依据

通过门控的方式：
- 估值是主要空头依据 -> 填 `forward_valuation` + 在 evidence 引用 Forward PE/PEG/一致预期数字
- 估值非主要依据 -> 填 `non_valuation`

### 4b. 置信度扣分

成长股 + 偏空 + 估值作为主要依据但未引用 Forward PE / PEG / 一致预期数字 -> **−2**（静态 PE 偏空误杀高成长标的）。

## 5. 强制证据

成长股多头论点：基本面证据 **≥ 2 条**，来自 `mx_data_query` 或 earnings 博查（营收/净利润趋势/ROE/毛利率/经营现金流）。基本面权重 40%，没有实质证据 = 空壳。

## 6. 案例

**603688**：AI 正确把 PE 偏高的标的降级为「均衡型」而非机械偏空 -- 体现「无法明确归类时不硬塞成长股、不凭高 PE 偏空」的纪律。

## 7. chat 侧用法（仅 chat）

用户问「这只成长股 AI 为什么这么判 / 该不该偏空 / 估值合理吗」时：

1. 调 `get_stock_analysis(ts_code, detail=full)` 拿 verdict + `stock_type` + `valuation_basis` + evidence
2. 对照本规则解释：
   - `stock_type=成长股` + 偏空 + `valuation_basis=forward_valuation` -> 分析遵循了禁静态 PE 规则，引用了前瞻估值
   - `stock_type=成长股` + 偏空 + `valuation_basis=static_pe_only` -> **不应出现**（硬门控会拦），若历史记录里出现说明该次分析在门控加上前发生
   - `stock_type=均衡型` 但用户认为是成长股 -> 讨论「为什么 AI 没归成长股」，引用识别特征表
3. 不要臆造 Forward PE/PEG 数字 -- 若 evidence 里没有，如实说「该次分析未引用前瞻估值」
