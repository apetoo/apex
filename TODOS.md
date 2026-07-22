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
 