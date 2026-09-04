# 自适应详细分析报告：最终契约修复报告

验收日期：2026-09-04

本次修复对应 `final-rereview.md` 的两项 Important 问题。未改动方向裁决、证据计分、置信度校准、交易价位算法、持仓报告、legacy verdict 报告或前端。

## I1：历史仅用于复盘与校准

- 正式报告历史 authority 的 `forecast_outcome` 白名单仅保留 `outcome`、`matured_at`、`hit` 和 `policy_version`。
- `return_pct`、`stock_return_pct`、`benchmark_return_pct`、`excess_return_pct`、`horizon_trading_days` 和 outcome 内重复的 `verdict` 不再进入正式报告上下文。
- 正式报告节点会再次通过 `build_report_context()` 清洗调用方传入的 history，因此直接调用 `_run_langgraph_loop()` 也不能绕过历史白名单。
- 提示词明确规定历史只能出现在 `### 历史判断复盘` 与 `### 置信度调整`，只能用于事后复盘和置信度校准；命中、未命中或结果标签不得支持本次方向、净硬度、交易动作或价位。
- 确定性校验禁止 history-only 的精确值进入核心判断、四维分析、多空论点、裁判结论、加权评分或操作建议。英文/标识符按完整 token 匹配，短中文通用方向词不做宽泛子串拦截；若同一值也存在于当前 authority，则不视为 history-only。
- `final-rereview.md` 的探针“历史样本前向收益为负，直接证明本次应继续观望偏空”现在会因引用未授权收益幅度而被拒绝。

## I2：authority 与章节内容一致

- `market`、`history`、`playstyle`、`evidence_selection` 非空时，对应章节必须包含可见正文，不得使用“无可靠数据 / 无历史样本 / 无数据”等独立或句首空态声明；存在可比较的非平凡标量值时，必须呈现至少一个精确 authority 值。
- 对应 authority 为空时，章节必须只包含一条简短空态句；真实空态仍兼容。
- 空对象嵌套（例如空 `profile`、空 `fit` 和 `risk_level: null`）仍视为空；`hit: false` 这样的布尔结果仍是非空 authority。
- “部分指标无数据；可用市场状态为 …”这类局部缺失说明不会被误判为空态，只要章节仍呈现可用 authority 值。
- “本次无可靠数据，但 …”后附 authority token 不能规避空态冲突校验。
- 校验基于 CommonMark 解析后的可见正文，不能用 fenced code、HTML 或其他非渲染内容满足契约。

这些检查只覆盖可确定的字段权限、精确值出处、章节空态与可见正文，不承诺识别任意改写、语义推导或仅凭布尔结果生成的自然语言是否充分。历史语义用途由明确的提示词契约约束；没有加入宽泛的方向词子串猜测。

## TDD 轨迹

- 第一轮 RED：12 个目标断言失败，覆盖历史收益字段仍泄漏、生产 `run()` 仍传播收益幅度、四类非空 authority 的空态占位仍被接受、空 authority 未强制空态、history-only 值仍可进入当前方向章节，以及 600487 风格提示仍暴露收益字段。
- 第一轮 GREEN：上述断言全部通过。
- 边界加固 RED：短历史标签 `bear` 未被拦截，且“部分指标无数据”被宽泛空态匹配误拒。
- 边界加固 GREEN：完整 ASCII token 校验拦截 `bear`，句级空态匹配允许局部缺失说明，同时避免 `偏空` 与当前 `观望偏空` 的子串误报。
- 最终边界 RED/GREEN：先复现并修复空嵌套对象被误判非空、仅布尔 authority 接受空正文，以及“无数据，但 …”追加 token 的矛盾空态绕过。

## 验证结果

- `tests/test_report_context.py tests/test_analysis_graph.py`：最终 `140 passed`。
- 预加载主工作区既有 `config.yaml`、允许 localhost 的全量 Python：`851 passed, 2 failed`（在最后增加一项矛盾空态回归前运行）。两项失败是复审已记录的 sector-sentiment PII 脱敏基线问题：`test_creator_metrics_use_all_unique_content_duplicate_is_time_stable_and_evidence_is_redacted` 和 `test_legacy_persisted_evidence_is_minimized_and_redacted_on_upgrade`。
- 最终代码再次运行全部 Python，仅排除上述两项基线失败：`852 passed, 2 deselected`。包含完整 Eastmoney 测试，不再有 localhost 绑定错误。
- `UV_CACHE_DIR=/private/tmp/apex-final-contract-uv-cache uv lock --check`：通过，解析 77 packages。
- `pyproject.toml`、`requirements.txt`、`uv.lock`：无变更。
- 前端：无文件变更，因此未重复运行前端测试、lint 或 build。
- `git diff --check`：通过。

## 变更文件

- `apex/report_context.py`
- `apex/analyze.py`
- `tests/test_report_context.py`
- `tests/test_analysis_graph.py`
- `final-contract-fix-report.md`
