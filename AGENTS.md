# AGENTS.md

给自治 coding agent（Hermes / Codex / Claude 等）的 apex 项目交接。**完整架构与约定以 `CLAUDE.md` 为准（SSOT）**，本文件只做导向 + agent 行为红线。

> ⚠ 早期文档里 "main.py 不存在 / streamlit 是主入口" 已失效：Streamlit 在 commit `675807e` 退役，`main.py` click CLI 现已存在，UI 是 FastAPI 后端 + Vite 前端。

## apex 是什么
个人 A 股交易闭环：DeepSeek 分析 + watchlist + vectorbt 回测 + 盘后模拟撮合 + 交易系统自检。单用户、文件存储、无 DB。

- **后端** FastAPI `:8000`，路由全挂 `/api`（`backend/main.py`）
- **前端** Vite + React（`frontend/`），dev 代理 `/api` -> `127.0.0.1:8000`
- **存储**（都在 `$HOME`，仓库外）：`~/.stock-journal/<ts_code>.jsonl`、`~/.stock-watchlist/watchlist.json`、`~/.stock-trades/trades.jsonl`、`~/.stock-closed/closed_positions.jsonl`

## 已经自动化的（别重复自动化）
**日线循环**：晨报 -> 盘中分析×3 -> 盘后筛选 -> 模拟撮合，由 `apex/automation.py` 的 cron/loop 自己跑（`SCHEDULE` + `run_once()` + `loop()`），触发监控在 `apex/monitor.py`。
agent 的活是 **meta 环**：月度复盘、纪律解读、提案——**不是日线交易**，别去接管 `automation.py` 调度。

## agent 能用的入口
- **CLI**：`python main.py {analyze, backtest, briefing, watchlist, promote, realtime, screener}`（别名 `an/bt/ls`）
- **后端 HTTP（更全）**：`http://127.0.0.1:8000/api/...`，端点见 `backend/routers/`。复盘类（`postmortem` / `calibration` / `system`）只有 HTTP、没 CLI。
- **复盘 skill**：`hermes/skills/monthly-review/`（Hermes 下 `/monthly-review`）

## 硬红线（任何 skill 都不许越）
1. 不改 `config.yaml`、不调策略权重 / 因子开关 / screener 权重。策略变更 = 写提案到 `proposals/`，人审。
2. 不动持仓：不 `promote` / `buy` / `sell` / `close` / `archive` / 增删 `candidates`。这些走人审。
3. 真实下单（券商 API）永远不交给 agent，只做 dashboard 提醒。
4. 样本 `n < 30` 不下结论（ADR-0002）；OOS 反过拟合优先于历史拟合。
5. AI 守规双层（ADR-0001）：`MANDATORY_SEARCH_CATEGORIES = [earnings, shareholders, regulatory, money_flow]` 4 类必搜，别为省调用次数放宽 `record_verdict` 校验。

## 三层权限
- **Tier 1 - 自动**：只读分析（backtest / postmortem / calibration / system / screener 查询 / briefing / watchlist 查询）
- **Tier 2 - 提案**：策略 / 参数 / calibration 调整 -> `proposals/`，人审才生效
- **Tier 3 - 人 only**：真钱开关、仓位 sizing、回撤 kill-switch

## 操作类 vs 策略类 skill（自我改进的边界）
- **操作类**（怎么跑复盘、怎么读报告、怎么格式化推送）：允许自我改进。
- **策略类**（选哪些因子、何时进场、权重多少）：**禁止自我改进、禁止自动新建**，只能由人维护。这是防止自校准闭环过拟合漂移的闸。
