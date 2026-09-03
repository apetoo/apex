# 自适应详细分析报告：最终修复报告

验收日期：2026-09-03

本次修复对应 `.superpowers/sdd/2026-09-01-adaptive-detailed-analysis-report/final-review.md` 的两项 Important 问题。未改动方向裁决、证据计分、置信度校准、价格建议、持仓报告或 legacy 报告行为。

## I1：最新历史与当前轮预测结果

- `build_report_context()` 现在先解析 `analyzed_at`，失败时回退到 `date`，统一带时区时间后按新到旧排序；同一时间戳按 journal 追加顺序稳定决胜，缺失或非法时间也有确定性顺序。
- 排序后才应用 `MAX_HISTORY=5`，因此不会再从 append-ordered journal 取最旧五条。
- 新增 `forecast_rows` 输入，按 `ts_code + 规范化分析时间` 连接本轮 `refresh_forecast_rows()` 返回值；等价时区表示可匹配，不同股票不会串联。
- `run()` 将当前轮刷新结果传入报告上下文，而不是只把它用于置信度校准。
- `forecast_outcome` 只保留前向结果的显式白名单标量字段，刷新结果中的草稿或任意附加字段不会进入模型提示。

## I2：被排除证据不再暴露论点内容

- `evidence_selection.excluded` 仅保留 `evidence_id`、`dimension`、`nature`、`as_of`、`frequency` 和 `reason`。
- `stance`、`hardness`、`adjusted_hardness`、`inference`、`fact`、`body` 及其他原始字段全部移除，模型无法从权威上下文取得被排除论点并作方向性改写。
- 正式报告契约补充禁止从安全元数据或排除原因推断、复原、转述被排除证据的事实、推论或方向。
- 原有 counted evidence ID 所有权、excluded ID 禁用、方向论点逐字 provenance、证据覆盖率、净硬度、置信度、价格和章节验证均保持启用。

## TDD 与回归验证

- RED：四个目标回归最初按预期失败，分别暴露不接受 `forecast_rows`、历史仍为旧序、excluded 内容仍泄露、生产 `run()` 仍传入最旧五条。
- GREEN：上述四个目标回归全部通过。
- 报告上下文与分析图完整测试：`123 passed`。
- 重点安全/传播筛选：`65 passed, 58 deselected`。
- 原始全量 Python：`826 passed, 2 failed, 9 errors`。两项失败与 final review 记录一致，均为既有 sector-sentiment PII 脱敏问题；九项错误均为 sandbox 禁止 `test_eastmoney_guba.py` 绑定 localhost。
- 排除 sandbox 不兼容文件后的 Python：`769 passed, 2 failed`，仍仅为上述两项既有隐私失败。
- 再排除两项已确认基线失败：`769 passed, 2 deselected`。
- 前端：`23 files / 206 tests passed`；lint 退出码 0（7 条既有 warning）；production build 成功（仅有既有 chunk-size warning）。
- `UV_CACHE_DIR=/private/tmp/apex-review-uv-cache uv lock --check`：通过，解析 77 packages。
- `git diff --check`：通过。

## 变更文件

- `apex/report_context.py`
- `apex/analyze.py`
- `tests/test_report_context.py`
- `tests/test_analysis_graph.py`
- `final-fix-report.md`
