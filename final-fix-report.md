# 板块情绪财经检索：最终修复验收

验收日期：2026-08-11

本次仅核验并收尾既有实现；未做重构。`frontend/package-lock.json` 是工作树已有的无关改动，未纳入提交。

## 最终审查 10 项

1. **批准时间截点**：批准时间只在首次批准时写入；作者任务携带该截点，采集器和入库边界均会拦截批准前内容、其后评论及缺失/非法时间。覆盖 `test_creator_approval_timestamp_is_immutable_and_carried_as_job_cutoff`、`test_creator_published_at_cutoff_is_rechecked_at_ingress_and_invalid_dates_quarantined`、`test_creator_comment_cannot_reintroduce_a_preapproval_parent_content`。
2. **交易日 cohort**：显式 `trade_date` 独立于 UTC 采集时间写入，策略 cohort 按交易日冻结/计数。覆盖 `test_explicit_trade_date_is_persisted_independently_from_utc_collection_time`、`test_policy_freeze_uses_sixty_distinct_trading_dates_and_allows_next_cohort`。
3. **爬虫旗标与预算**：实际 CLI 带内容/评论上限、单并发、禁用子评论；SQLite 每日任务预算原子保留且失败不伪装覆盖率。覆盖 `test_runner_passes_real_cli_limits_and_uses_an_isolated_output_boundary`、`test_persisted_daily_job_budget_prevents_duplicate_collection`、`test_failed_daily_job_budget_is_not_retried_or_reported_as_covered`。
4. **三条相关性路径**：实体+金融证据、批准作者+投资语境、低置信 LLM 审核均具审计原因；不符合项过滤。覆盖 `test_financial_relevance_has_three_explicit_admission_paths_and_audit_reasons`。
5. **严格 LLM schema**：金融相关性与语义证据均拒绝未知字段、错误类型、非有限或越界数值、未知/重复板块和无原文证据。覆盖 `test_relevance_llm_rejects_unknown_wrong_type_and_non_finite_fields`、`test_semantic_llm_rejects_unknown_sector_fields_and_invalid_ranges`、`test_semantic_llm_rejects_duplicate_sectors_and_unbound_evidence_span`。
6. **查询契约与 scope 合并**：模板必须同时包含 `{term}` 和独立金融锚点；空词不可渲染，完全相同的平台查询会合并全部板块 scope。覆盖 `test_query_templates_require_term_and_a_separate_financial_anchor`、`test_blank_taxonomy_terms_never_render_a_bare_financial_query`、`test_equal_platform_queries_aggregate_every_matching_sector_id`。
7. **pin / 版本审计**：启用管道强制 MediaCrawler 固定提交和精确冻结版本；pin 脏工作树会拒绝采集，策略哈希持久化到运行、作者证据和审核事件。覆盖 `test_creator_runner_uses_process_local_bounded_adapter_and_rejects_dirty_pin`、`test_enabled_pipeline_requires_pin_and_exact_frozen_versions`、`test_frozen_policy_hash_is_persisted_on_runs_creator_evidence_and_events`。
8. **legacy telemetry latest**：迁移保留旧覆盖率、将缺漏斗表示为 `null`，并按最新日期/完成时间/rowid 取得最新行。覆盖 `test_legacy_collection_migration_preserves_coverage_null_funnel_and_latest_rowid`。
9. **API / UI 失败覆盖**：缺失作者经全局领域错误映射为 404；作者池加载失败可重试，审核失败保留行并显示提示，失效作者展示采集告警。覆盖 `test_creator_not_found_uses_the_global_domain_error_mapping`、前端 `shows a retryable error...`、`keeps a creator row...`、`renders collection failure...`。
10. **PII 脱敏**：送入相关性 LLM、持久化证据和公开 API 均删除联系方式、内容 ID 与本地异常路径。覆盖 `test_relevance_llm_never_receives_contact_details`、`test_creator_api_returns_redacted_minimal_evidence_without_content_ids`、`test_creator_api_does_not_expose_collection_exception_paths`。

## 验证结果

- `PYTHONPATH=. /Users/wanmingyu/workspace/my/apex/.venv/bin/python -m pytest -q tests/test_sector_sentiment_hardening.py tests/test_sector_sentiment.py tests/test_sector_sentiment_api.py`：96 passed。
- `PYTHONPATH=. /Users/wanmingyu/workspace/my/apex/.venv/bin/python -m pytest -q`：389 passed，1 条既有 FastAPI/Starlette deprecation warning。
- `npm test -- --run`：18 files / 178 tests passed。
- `npx tsc -b`：passed。
- `npm run lint`：exit 0，7 条既有 warning。

## 仍需关注

`npm run build` 的 Vite 打包阶段失败，原因是此独立 worktree 缺少 `frontend/index.html`；主工作目录存在该文件。本次任务要求的前端测试、TypeScript 编译与 lint 均已通过，且未复制或改动该工作树结构文件。
