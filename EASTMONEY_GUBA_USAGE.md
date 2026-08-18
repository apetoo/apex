# 东方财富股吧情绪采集使用说明

该功能把东方财富股吧的公开帖子和一级评论接入板块散户情绪系统。默认以 `shadow` 模式运行：数据会采集、脱敏、保存并展示质量遥测，但不会改变现有 Bilibili/抖音评分、覆盖率、预警状态机或冻结策略哈希。

> 仅用于个人研究，不构成投资建议。采集器只访问公开免登录页面，不登录、不携带 Cookie、不绕过验证码，也不使用代理轮换。

## 1. 准备环境

在仓库根目录安装项目依赖：

```bash
npm run setup
```

复制本地配置模板：

```bash
cp config.example.yaml config.yaml
```

代表成分股的正常运行路径需要：

- AkShare：读取东方财富概念/行业板块成分股；
- Tushare Token：读取交易日历和日成交额；
- 最近至少 20 个交易日的完整成交额数据。

`config.yaml` 已被 Git 忽略。不要提交 Token、Cookie、作者盐值或账号信息。

## 2. 配置

在 `config.yaml` 中启用板块情绪和东方财富采集。下面是可执行的东方财富关键配置片段，应在复制出的完整 `config.example.yaml` 上修改，不能用它替换模板中的其他字段。

```yaml
sector_sentiment:
  enabled: true
  cache_dir: "~/.stock-sentiment"

  # shadow 阶段仍只把原有短视频平台用于正式评分。
  platforms: ["bili", "dy"]

  eastmoney:
    enabled: true
    phase: shadow
    promotion_override_reason: ""
    access_mode: public

    lookback_hours: 24
    overlap_hours: 2
    constituent_count: 5

    posts_per_sector_forum: 100
    posts_per_constituent_forum: 50
    comments_per_post: 50
    concurrent_requests_per_domain: 1
    download_delay_seconds: 2
    timeout_seconds: 20
    collection_timeout_seconds: 900
    retry_times: 3
    circuit_breaker_failures: 5
    max_requests: 500
    requests_per_target: 100

    minimum_posts: 1
    minimum_authors: 1
    minimum_parse_success_rate: 0.8

    schema_version: "eastmoney-public-v1"
    target_pool_version: "eastmoney-target-v1"
    author_salt: "替换为仅保存在本机的随机值"

    endpoints:
      list_url_template: "https://guba.eastmoney.com/list,{forum_id}.html"
      list_next_url_template: "https://guba.eastmoney.com/list,{forum_id}_{page}.html"
      detail_url_template: "https://guba.eastmoney.com/news,{forum_id},{content_id}.html"
      comments_url_template: "https://guba.eastmoney.com/api/getData?code={forum_id}&path=reply/api/Reply/ArticleNewReplyList&postid={content_id}"

  taxonomy:
    - sector_id: "concept:robot"
      sector_name: "机器人"
      taxonomy: "concept"
      aliases: ["机器人", "人形机器人", "具身智能"]
      eastmoney_forum_id: "bk0910"
```

注意：

- `eastmoney_forum_id` 必须人工核验，系统不会猜测板块吧 ID。
- 完整的 `run_configured` 还会执行配置中的 Bilibili/抖音采集：先在仓库外安装 MediaCrawler，把 `sector_sentiment.mediacrawler_path` 改为其实际路径，并把 `mediacrawler_commit` 填为 `git -C <path> rev-parse HEAD` 输出的完整 40 位十六进制 commit。东方财富自身不使用 MediaCrawler，仍只访问公开免登录页面。

### 提前迁移冻结的采集策略

更新 MediaCrawler 提交或调整 Bilibili/抖音采集配额会改变采集策略哈希。当前 cohort 未满 60 个交易日时，可在 `sector_sentiment` 下临时填写一次带审计原因的迁移配置：

```yaml
policy_override_reason: "升级 MediaCrawler 并降低 B站/抖音采集配额"
```

原因必须是非空字符串且不超过 500 个字符，同一原因只能授权一次迁移。迁移只允许从最新历史日期之后的新交易日开始新 cohort，不会改写或删除历史，同一天已预约的策略也不能覆盖。首次运行完成策略预约后可以删除该配置；新的 cohort 会继续遵守正常的 60 个交易日冻结。
- 每个板块还会按最近 20 个交易日成交额选择 5 只代表成分股，并抓取对应个股吧。
- 少于 5 只、行情不可用或 provider 失败时，manifest 会记录 `target_shortfall`，当次结果不会被认定为质量达标。
- 不要把 `eastmoney` 加入 `platforms` 来提前参与评分。是否纳入正式评分由 `phase` 控制。

## 3. 手动运行一次

以下命令运行完整的板块情绪采集流程，其中包括配置中启用的 Bilibili、抖音和东方财富数据源：

```bash
.venv/bin/python -c \
  "from apex.sector_sentiment import run_configured; import pprint; pprint.pp(run_configured())"
```

如使用已激活的虚拟环境，也可以把 `.venv/bin/python` 替换为 `python`。

东方财富由独立 Scrapy 子进程执行。父进程会等待采集报告并统一完成数据入库、评分隔离和审计记录。

## 4. 自动运行

东方财富采集已经接入盘后 `sector_sentiment` 自动化任务：

```bash
# 按当前交易时段运行一次
.venv/bin/python -m apex.automation

# 常驻调度，到盘后时段自动运行
.venv/bin/python -m apex.automation --loop

# 查看当天自动化状态
.venv/bin/python -m apex.automation --status
```

自动化目前仅在周末跳过；法定节假日若落在工作日，仍可能运行。自动化状态保存在 `~/.stock-journal/automation_state.json`。

## 5. 查看结果

启动前后端：

```bash
npm run dev
```

然后访问：

- 前端板块情绪页：`http://localhost:5173/sector-sentiment`
- API 总览：`http://localhost:8000/api/sector-sentiment/overview`
- API 文档：`http://localhost:8000/docs`

默认缓存目录为 `~/.stock-sentiment`。主要产物包括：

```text
~/.stock-sentiment/
├── sector_sentiment.sqlite3
└── raw/
    ├── eastmoney/
    │   ├── latest.json
    │   └── latest-success.json
    └── YYYY-MM-DD/eastmoney/<batch-id>/
        ├── manifest.json
        ├── records.jsonl
        ├── collector-report.json
        └── telemetry.json
```

其中：

- `manifest.json`：冻结的目标、分页模板、窗口下界，以及代表股来源、20 日成交额和 as-of；
- `records.jsonl`：脱敏后的标准记录，帖子和评论都带真实公开证据 URL；
- `collector-report.json`：Scrapy 请求、解析、配额和失败目标计数；
- `telemetry.json`：本批次不可变审计，包括质量、shortfall 和 promotion audit；
- `latest-success.json`：上一份成功批次指针，用于当前失败时的 stale 展示。

## 6. 时间窗口和分页语义

- 默认窗口为最近 24 小时，并增加 2 小时重叠，实际下界为 26 小时前。
- 列表会受控向后翻页，直到达到窗口下界、该目标帖子上限或全局请求配额。
- 正文早于窗口的旧帖不会再次计入帖子热度。
- 若旧帖的最后活动时间仍在窗口内，会继续调度一级评论抓取。
- 时间字段缺失/异常或页面结构变化会触发 `schema_changed`/降级，不会无界翻页。

## 7. Shadow 与 Promoted

### Shadow（默认）

```yaml
eastmoney:
  enabled: true
  phase: shadow
```

此阶段东方财富只用于审计和质量观察，不参与正式评分、覆盖门槛或状态机。只有独立日期且满足帖子数、作者数、解析率和代表股完整性要求，才累计为“质量达标日”。

### Promoted

系统门禁为至少 14 个质量达标 shadow 日期后，才可切换：

```yaml
eastmoney:
  phase: promoted
  promotion_override_reason: ""
```

未满 14 个合格日时，系统默认拒绝 promoted。确需人工 override，必须填写明确审计原因；无论是否满足门禁，切换前人工复核冻结 cohort、采集质量和证据链接都是建议的操作步骤：

```yaml
eastmoney:
  phase: promoted
  promotion_override_reason: "人工复核采集质量、证据链接和目标覆盖后批准"
```

override 原因保存在本地审计中，但 API 只公开 `override_used`，不会返回原因正文。切换 promoted 会建立新的评分策略 cohort，东方财富才进入财经社区覆盖门槛。

## 8. 失败与 stale 展示

当前 promoted 采集失败时：

- 当前失败仍写入审计和遥测；
- 页面继续展示上一份成功的报告/评分，并标记 `stale` 与“评分展示截至”日期；
- 当前状态保持 `degraded`/`insufficient_data`，不会把失败解释为低风险；
- 如果没有上一份成功评分，则只展示当前失败和数据不足状态。

常见状态：

| 状态 | 含义 |
|---|---|
| `ok` | 采集完成且有有效记录 |
| `empty_valid` | 窗口内无新增内容，但请求和解析正常 |
| `partial` | 部分目标失败或达到配额 |
| `degraded` | 代表股不足或采集质量不足 |
| `blocked` | 页面返回访问限制/验证码 |
| `schema_changed` | 页面结构或关键时间字段不符合预期 |
| `stale` | 当前失败，展示上一成功批次或评分 |

## 9. 排障

### `representative_provider_unavailable`

检查 Tushare Token、网络、交易日历和 AkShare 板块成分接口。系统会保留显式 shortfall，不会静默退化为只抓板块吧。

### `target_shortfall`

查看对应批次 `manifest.json` 的 `representative_constituents`。重点检查 `selected_count`、`membership_source`、`turnover_source`、`turnover_as_of` 和 `reason`。

### `blocked`

停止提高频率并稍后重试。该采集器不会处理验证码，也不应通过登录、Cookie、代理轮换或验证码绕过继续访问。

### `schema_changed`

停止生产 promotion，保留当前审计，并使用保存的上一成功评分。确认东方财富公开页面结构后再更新解析器和回归测试。

### 查看本地测试

```bash
.venv/bin/python -m pytest -q tests/test_eastmoney_pipeline.py
.venv/bin/python -m pytest -q tests/test_eastmoney_guba.py
NUMBA_DISABLE_JIT=1 PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_sector_sentiment_api.py
cd frontend && npm test -- --run
```
