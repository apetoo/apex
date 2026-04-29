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
