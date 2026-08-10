# TODOS

## Open

### tushare Pro 权限探测
**What:** SKILL.md 的 environment check 或 Wave 2 的 data-sources.md 里加一段"首次运行时试探高权限接口（top_list, margin_detail 等），缺权限则降级到基础接口"。
**Why:** tushare 不同积分等级能用的接口不同，用户的 token 权限不明确。如果 SKILL.md 写了"拉龙虎榜"但用户没权限，会在分析中途报错。
**Pros:** 避免运行时报错打断分析流程。
**Cons:** 首次运行多花 10 秒做探测。
**Context:** 设计文档 Open Question 5 已提到。落地时机：Wave 2 写 data-sources.md 时同步把降级规则写进 SKILL.md 的 workflow。
**Depends on:** Wave 2 开始后。

### 日志备份策略
**What:** 给 ~/.stock-journal/ 配一个简单的备份方案（cron rsync 到 iCloud Drive 或外部存储）。
**Why:** stock-journal 是判断历史的证据，用于复盘和 eval。丢了就没法回测。
**Pros:** 数据安全。
**Cons:** 需要一次性配 cron，5 分钟的事。
**Context:** 设计文档 Distribution Plan 提到"建议 cron 每周 rsync 到 iCloud"。落地时机：Wave 3 日志功能上线后。
**Depends on:** Wave 3 完成后。

### v1.1 契合度 mismatch 规则（Playstyle Engine）
**What:** 实现 `compute_playstyle_fit` 的 4 档 mismatch 规则（打野/波段/中线/长线阈值），含三项被 v1 评审推迟的修复：A1 `stop_honored_rate` 字段路径（`ai_adherence.aggregate.stop_honored_rate`，非 `discipline_score.aggregate`）+ None 守卫；C1 per-field confidence 门控（每规则查自己 metric 的 confidence，非"画像 confidence"）；OV#5 用 `hold_period_distribution` 桶（system.py:313，判"是否曾持有>20d"）替代单一 `avg_hold_days` 均值。
**Why:** v1 评审（D10/OV#4）发现当前闭环样本 n<20，按 per-field 门控 `playstyle_fit` 对所有票返回 `insufficient_data`，4 档规则几个月内不触发也无法验证。v1 只留 `playstyle_fit` 字段脚手架（始终 insufficient_data，UI 显示"画像累积中"），规则推迟到样本达标后建并真实验证。现在建是过早投入（逻辑建出也无处验证）。
**Pros:** 规则在能真实触发和验证时再建；A1/C1/OV#5 三项修复随规则一起在 v1.1 建并验证，不建不存在的 bug。
**Cons:** 契合度从 v1 交付物降为 v1.1；设计定位的"Playstyle Engine 第一个消费者"推迟。
**Context:** 设计文档 `~/.gstack/projects/apex/wanmingyu-v9-design-20260714-105554.md` Review Decisions 节 D10/OV#4。触发条件：用户平仓数 n≥20（system.py `_desc_flag` ok 阈值），`avg_hold_days.confidence` / `ai_adherence.aggregate.confidence` 达 ok。v1 的 `playstyle_fit` 字段脚手架已就位，v1.1 只加规则逻辑不改字段形状。注意：若 D9 prototype 走 Python 规则路径（星级确定性算出），契合度仍消费 Python 星级，规则独立于星级来源。
**Depends on:** Playstyle Engine v1 上线（分类器验证通过）+ 用户平仓 n≥20。

### v1.1 评论质量评分 + regime/session 上下文（散户画像 Crowd Behavior）
**What:** 两块 eng-review 推迟到 v1.1 的工作：(1) `score_comment_quality`（likes/replies/有逻辑/纯情绪/重复，规则为主 + LLM 辅助判逻辑）+ `compute_indicators` 改质量加权；(2) regime 上下文（主升浪 momentum regime 下 FOMO 方向对，contrarian 不可靠时显式标注）+ session 分桶（盘前 9:30 前 / 盘中 / 盘后 15:00 后 FOMO 含义不同，v1 扁平 [00:00,23:59] CST）。
**Why:** v1 用原始计数 + 扁平日桶（eng-review OV5/C1/OV6/OV7 决定）。质量加权是二阶精修，2 周验证期内原始计数更能快验假设；regime/session 是 contrarian 假设的 regime-dependent 风险（OV6：主升浪 FOMO 方向对，panic=底不成立），v1 接受扁平化，v1.1 加上下文护诚实。
**Pros:** 质量加权让高逻辑评论权重大于"冲！""垃圾！"；regime 上下文护 contrarian 在 momentum regime 不误报；session 分桶区分盘前/盘后情绪语义。
**Cons:** 质量评分需调参 + LLM 辅助判"有逻辑 vs 纯情绪"；regime 检测需定义（如 MA 多头 + 连续涨 N 日 = 主升浪）；session 分桶需股吧 publish_time 精确到时段（股吧时间戳是 CST）。
**Context:** design doc `~/.gstack/projects/apex/wanmingyu-v9-design-20260718-155154.md` Eng-Review Findings 节 OV5（热门票采样）/ C1（Contrarian 待校准）/ OV6（regime-dependent）/ OV7（session 语义）。Contrarian 公式 + ≥70 阈值 v1 标"待校准"，v1.1 连同 regime 上下文一起校准（5-8 只实盘票基准集，pre-log 命中率 vs 朴素基准）。
**Depends on:** 散户画像 v1 上线 + Step 0.5 千股千评先验 go（2 周 pre-log 验证"任何 crowd 信号能预测反转"成立）+ 2 周 pre-log 验证 crowd text 信号有用。若 Step 0.5 no-go 则整个 crowd 不建，此 TODO 作废。

### technical.py 既有函数测试 backfill
**What:** 给 `apex/technical.py` 既有函数补单测（mock `pro.daily` 返回固定 DataFrame，断言 `_compute_bars` 的 tail(60)/min_bars 行为、`fetch_bars`/`batch_fetch_bars` 的缓存行为、`ma_distance`/`atr_14`/`realized_vol` 等指标计算）。
**Why:** 2026-08-07 缠论页 eng-review 发现该文件在 tests/ 零引用零覆盖，而 screener 技术面策略全建立在它上面——指标算错不报错，只是静默降低选股质量。
**Pros:** 补上核心模块零覆盖；`_raw_daily` 的 fixture 与缠论 PR 新增 `fetch_raw_bars` 的测试天然共享。
**Cons:** 指标期望值需手算或从实现反推（有"测试抄实现"风险）；与任何单一功能 PR 无绑定，需要专门排期。
**Context:** 缠论 PR（design `~/.gstack/projects/apex/wanmingyu-develop-design-20260807-133032.md`）只测新增/触及的代码，本 TODO 覆盖既有部分。评审结论：不在缠论 PR 里顺手做，保持 PR 右尺寸。
**Depends on:** 无。可在缠论页 v1 落地后任何时间做。

### BJ 旧代码自动重映射（83/88xxxx → 920xxx）
**What:** `normalize_ts_code` 或 `migrate_and_backfill` 加北交所旧码 → 920 新码重映射（用 tushare stock_basic 对照表，非盲映射后三位）；存量 watchlist/journal 里的旧 .BJ 代码一并迁移。
**Why:** 2026-08-07 缠论 day-0 实测：2025-05 北交所代码迁移后旧 43/83/87/88xxxx 在 tushare 全部返回 0 行（"pro.daily 不支持 BJ"的旧结论实为旧码已废）。920+后三位规则在 4 只票上验证成立，但盲映射有碰撞风险（不同旧前缀可能撞同一新码），需官方对照。当前 normalize 规则 4/8→BJ 对已迁移票产生**静默空数据**。
**Pros:** 修掉 BJ 票全链路（行情/分析/缠论页）的静默空数据；watchlist 里若有旧 BJ 码可自愈。
**Cons:** 需要拉全量 stock_basic 建对照表；BJ 票在用户实际持仓/候选中的占比未知，可能零收益。
**Context:** 缠论 day-0 发现（design doc Day-0 Spike 结果节）。T2 只加了 9→BJ 新码识别，本 TODO 覆盖旧码迁移。
**Depends on:** 确认 watchlist/journal 里是否真有 .BJ 旧码条目（没有则降级为纯防御性）。
 