<div align="center">

# apex - A股 AI 交易闭环系统

个人 A 股量化工具：DeepSeek AI 分析 + 持仓追踪 + vectorbt 回测 + 盘后粗筛 + 模拟实盘自动化。

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.12%2B-3776AB.svg)
![node](https://img.shields.io/badge/node-18%2B-339933.svg)
![react](https://img.shields.io/badge/React-19-61DAFB.svg)
![fastapi](https://img.shields.io/badge/FastAPI-backend-009688.svg)
![status](https://img.shields.io/badge/status-personal%20project-orange.svg)

</div>

> ⚠️ **免责声明**：本项目仅供学习研究与技术交流，**不构成任何投资建议**。A 股投资有风险，使用者需独立判断并自行承担一切交易后果。本项目按「现状」提供，不提供任何明示或暗示的担保。详见文末[完整免责声明](#完整免责声明)。

---

## 简介

apex 是一套面向个人投资者的 A 股交易闭环系统，把「AI 分析 -> 候选追踪 -> 实盘建仓 -> 收盘复盘 -> 校准反哺」串成一条可回测、可审计的链路。单用户工具，文件存储，无数据库。

解耦三层架构：

- **`apex/`** - 服务层（数据 / 分析 / 回测 / 筛选 / 自动化 / 情绪面 / 散户画像 / 玩法判定），无框架、无 DB
- **`backend/`** - FastAPI 薄路由层，挂在 `apex.*` 之上，统一 `/api` 前缀
- **`frontend/`** - Vite + React UI（雪球风，红涨绿跌），走 `/api` 调后端

## 特性

- 🤖 **AI 分析闭环**：DeepSeek（OpenAI 兼容）函数调用，强制 4 类网搜（财报 / 股东 / 监管 / 资金面）打底，verdict + 嵌套价格建议 + 证据链，写入 append-only journal
- 📡 **流式分析**：SSE 实时回放工具调用 trace，前端自写 `useSSE`（避免自动重连导致重复分析 / 重复写盘）
- 📊 **vectorbt 回测**：每个看多信号一个 `Portfolio.from_signals`，真实佣金 / 印花税 / 滑点扣减，含生存者偏差 / 基准错配 / OOS 反过拟合等可信度修复
- 🧹 **盘后粗筛**：龙虎榜 / 涨停 / 概念 / 北向 / 行业多策略规则层 + 可选 AI 二次综合，权重滑块本地保存
- 🤖 **模拟实盘自动化**：盘后 OHLC 回放撮合（T+1、成交价=触发价、状态机幂等），cron 友好
- 📈 **分时看板**：腾讯分时源（7 只 0.19s），明文不限流；个股分时特征预注入 prompt（不让 AI 决定调不调）
- 🌡️ **市场情绪面**：东财 4 池子 -> 三维度 score + regime + market_style，注入式（非 AI 工具）
- 🧑‍🤝‍🧑 **散户画像 / 玩法判定**：千股千评先验层 + 标的玩法（打野/波段/中线/长线）+ 玩家契合度三态
- 📋 **持仓 / 候选 / 归档**：candidate -> position 流程（分析不直接建仓），软删除，收盘触发 AI 复盘 + 重新校准
- 🛡️ **守规与样本门控**：双层守规（ADR-0001）+ 样本门控披露（ADR-0002），`/system` 页可视化「我的交易系统」
- 🔌 **MCP 接入**：内置 MCP server（stdio），让 Claude Code / Codex / Cursor 等外部 agent 远程读 watchlist / journal / 行情，执行加候选 / 转持仓 / 平仓 / 分析 / 粗筛 / 回测

## 快速开始

```bash
# 1. 克隆 + 配置
git clone https://github.com/apetoo/apex.git
cd apex
cp config.example.yaml config.yaml   # 填入你的 tushare / deepseek / bocha token

# 2. 装依赖（Python + Node 一条龙，含创建 .venv）
npm run setup

# 3. 起前后端（一条命令，Ctrl-C 一起停）
npm run dev
```

- 前端: http://localhost:5173
- 后端 API 文档: http://localhost:8000/docs （`/api` 由 Vite proxy 转发到 8000）

> 需要自备 [tushare](https://tushare.pro/) token 与 [DeepSeek](https://platform.deepseek.com/) API Key。详见[配置](#三配置)。

---

## 一、环境准备

- **Python ≥ 3.12**
- **Node.js ≥ 18**（前端）
- **[uv](https://docs.astral.sh/uv/)** - 推荐的 Python 环境与依赖管理工具

安装 uv（已装可跳过）：

```bash
# macOS
brew install uv
# 或官方脚本
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 二、安装

> 一键装：`npm run setup`（= `uv venv` + `uv pip install -r requirements.txt` + `cd frontend && npm install`）。下面是分步说明。

### 后端（Python）

在仓库根目录：

```bash
# 1. 创建虚拟环境（.venv，自动选 Python 3.12+）
uv venv

# 2. 激活
source .venv/bin/activate

# 3. 安装依赖（完整依赖清单在 requirements.txt）
uv pip install -r requirements.txt
```

> 不想激活也可以直接用 `.venv/bin/python`、`.venv/bin/uvicorn` 运行。
> 依赖包括：`openai` / `tushare` / `akshare` / `vectorbt` / `pandas` / `numpy` / `pyarrow` / `click` / `pyyaml` / `fastapi` / `uvicorn` / `sse-starlette` / `pytest`。

验证语法（无测试套件时的兜底）：

```bash
python -c "import ast; ast.parse(open('apex/analyze.py').read())"
```

### 前端（Node）

```bash
cd frontend
npm install
```

---

## 三、配置

复制模板并编辑 **`config.yaml`**（已被 `.gitignore` 忽略，不会进版本库；启动时由 `apex.config.load()` 读一次，缓存为模块单例）：

```bash
cp config.example.yaml config.yaml
```

### Token / API Key

YAML 里的值**优先**；留空则回退到环境变量（`TUSHARE_TOKEN` / `DEEPSEEK_API_KEY` / `BOCHA_API_KEY` / `MX_APIKEY`）。

```yaml
tushare:
  token: ""                      # tushare.pro 注册获取（或 env TUSHARE_TOKEN）

deepseek:
  api_key: ""                    # platform.deepseek.com 获取（或 env DEEPSEEK_API_KEY）
  base_url: "https://api.deepseek.com"   # OpenAI 兼容端点；火山方舟等亦可
  model: "deepseek-chat"         # deepseek-chat=V3, deepseek-reasoner=R1
  max_tool_iterations: 12        # AI 工具调用循环上限
  history_limit: 8               # 分析时注入 prompt 的历史判断条数

bocha:
  api_key: ""                    # web 搜索（或 env BOCHA_API_KEY）

mx:
  api_key: ""                    # 妙想金融数据 API（或 env MX_APIKEY）
```

### 路径（默认不用改，均 tilde 展开到 `$HOME`）

```yaml
paths:
  journal_dir: "~/.stock-journal"                              # AI 分析日志目录
  watchlist_file: "~/.stock-watchlist/watchlist.json"          # 持仓 & 候选
  prompt_file: "apex/prompts/expert-persona.md"                # 分析师 persona prompt
  screener_dir: "~/.stock-journal/screener"                    # 粗筛报告
  screener_prompt_file: "apex/prompts/screener_quick.md"
```

### 回测

```yaml
backtest:
  lookforward_days: 10                # 每个看多信号向前回测的天数
  min_entries_for_analysis: 5         # 少于该条数不跑统计
  min_review_samples: 8               # AI 复盘最小可成交信号数
  max_inject_age_days: 30             # 复盘反哺 analyze 的最大时效
  oos_test_days: 30                   # OOS 验证：最近 N 天作 test 集
  costs:                              # 交易成本（回测扣减）
    commission_rate: 0.00025          # 佣金 万2.5（双向）
    stamp_duty_rate: 0.001            # 印花税 千1（卖出）
    slippage: 0.001                   # 单边滑点 10bp
```

### 通知

```yaml
notify:
  enabled: true
  channel: "console"          # console（终端打印）| bark（iOS 推送）
  bark:
    base_url: ""              # 例: https://api.day.app/<your-key>/
    group: "apex"            # bark 通知分组名,同组通知在通知中心折叠
```

### 代理（可选）

全局 HTTP 代理，影响 DeepSeek / tushare / bocha 等所有出站请求。留空不走代理；新浪行情接口代码内显式 bypass，不受影响。shell 的 `HTTPS_PROXY` / `HTTP_PROXY` 优先级高于此配置。

```yaml
proxy:
  http_proxy: ""              # 例: http://127.0.0.1:7890
  https_proxy: ""
  no_proxy: ""                # 例: localhost,127.0.0.1,.cn
```

### 粗筛（screener）

```yaml
screener:
  enabled: true
  ai_enabled: true            # 关掉则只跑规则层不调 AI
  ai_model: "deepseek-chat"
  top_n_for_ai: 15            # 送入 AI 的候选数
  rule_weights:               # 各类信号权重（龙虎榜/涨停/概念/北向/行业）
    dragon_tiger: 2.0
    limit_up: 1.8
    concept: 1.5
    northbound: 1.2
    industry: 1.0
  filters:
    min_float_mv_yi: 30             # 排除流通市值 < 30 亿
    exclude_st: true                # 排除 ST
    exclude_new_listings_days: 60   # 排除上市 60 天内新股
  concurrency: 8
```

### 持仓推送（push）

apex 主动调消费方 webhook，把持仓变更推出去（接口文档见 `docs/integration/positions-push-api.md`）。

```yaml
push:
  enabled: false                       # 默认关；填好 base_url 后改 true
  base_url: "http://127.0.0.1:8899/api/apex"
  path: "/positions/push"
  incremental:
    enabled: true            # 持仓变更实时推（push.enabled=false 时仍不发）
  log_file: "~/.stock-journal/push_log.jsonl"   # 推送审计日志
```

### 自动撮合 / 模拟实盘（auto_trade）

```yaml
auto_trade:
  enabled: false             # 本地人工环境保持 false；服务器部署改 true
  top_n_picks: 5             # 粗筛后分析几只（按 ai_score 降序）
  holding_period_days: 10    # 持有期满强制平仓（exit_reason=expired）
  bullish_verdicts: ["看多", "偏多"]   # 哪些 verdict 入候选
```

> 成交价 = `trigger_price`（口径一致）；T+1；单只失败不中断；幂等靠状态机。
> 已知简化：涨跌停 / 除权未还原，首版接受。

---

## 四、启动

```bash
npm run dev
```

`concurrently` 同时起前后端：后端 `uvicorn :8000`（绿）、前端 `vite :5173`（青），日志带前缀区分，Ctrl-C 一起停。Vite 把 `/api` 代理到 `127.0.0.1:8000`。

- 前端: http://localhost:5173
- 后端 API 文档: http://localhost:8000/docs
- 健康检查: http://localhost:8000/api/health

只想起一端调试：`npm run backend` / `npm run frontend`。

### 页面路由

| 路由 | 功能 |
|---|---|
| `/` | 概览（市场温度 + 持仓 + chat 触发） |
| `/watchlist` | 持仓 / 候选 / 归档三标签（价格 / 止损目标 / 盈亏 / 触发，内联新增表单） |
| `/analyze` | 个股分析（SSE trace 流式 + verdict 注入 chat + 个股历史概要） |
| `/journal` | 跨股票全量历史 + 搜索 + 点行抽屉看完整结果 |
| `/backtest` | 逐信号柱状图 + 统计表 + 已平仓交易 |
| `/screener` | 多策略粗筛（权重滑块 localStorage + SSE 进度 + AI 综合） |
| 右下角 | 全局 chat（单次注入当前页上下文） |
| `/system` | 「我的交易系统」（守规 + 样本门控 + 上下文） |

---

## 五、CLI 命令

入口 `main.py`（基于 click，支持别名 `ls`/`bt`/`an`）。运行前先激活 `.venv`。

```bash
# 分析单只股票（ts_code 必须带后缀，如 002050.SZ / 603019.SH / 838810.BJ）
python main.py analyze 002050.SZ
python main.py an 002050.SZ --no-save        # 别名 + 不落盘

# 每日晨报：持仓状态 + 候选触发检查
python main.py briefing

# 回测所有看多信号（或限定单只）
python main.py backtest
python main.py bt --ts-code 002050.SZ --detail

# 查看 watchlist（持仓 / 候选 / 归档概要，带实时价格）
python main.py watchlist
python main.py ls --no-realtime

# 查询实时行情（多只）
python main.py realtime 002241.SZ 002050.SZ

# 候选提升为持仓（实际成交后）：填实际成交价
python main.py promote 002050.SZ --entry-price 45.0 --stop-loss 42.0 --target 50.0

# 盘后筛选器（规则 + 可选 AI 二次筛选）
python main.py screener
python main.py screener --rule-only --top-n 20
```

> **ts_code 规范**：系统内部一律用带交易所后缀的形式（首 digit 决定：6->SH、0/3->SZ、4/8->BJ）。所有 API 入口都会先过 `data.normalize_ts_code()`；CLI 手输时最好直接带后缀。

---

## 六、自动化（`apex.automation`）

全自动交易助手，支持四种模式：

```bash
# 1. 单次：按当前时段执行对应任务（cron 友好）
python -m apex.automation

# 2. 长连：交易时段持续运行，到点自动干活 + 每 60s 盯触发价
python -m apex.automation --loop

# 3. 盘后一次性自动撮合 + 粗筛入候选（服务器 cron 用，需 auto_trade.enabled=true）
python -m apex.automation --auto-trade

# 4. 查看今天干了什么
python -m apex.automation --status
```

### 长连模式（`--loop`）时间表

| 时段 | 任务 |
|---|---|
| 09:00（morning_ready） | 晨报 + 持仓 / 候选触发检查 |
| 09:30–11:30（morning） | 盘中分析 #1（从候选 / 归档挑一只） |
| 13:00–15:00（afternoon） | 盘中分析 #2 |
| 14:30–15:00（close） | 尾盘分析 #3 |
| 15:30（postmarket） | 盘后筛选器 |
| 全天交易时段 | 每 60s 盯触发价（复用 monitor，命中即推送） |

非交易日跳过；状态记录在 `~/.stock-journal/automation_state.json`（按天重置，幂等防重复）。

### 自动撮合（`--auto-trade`，模拟实盘）

需 `config.auto_trade.enabled=true`，否则跳过。流程：

1. **先平仓**：`entry_date < 今日` 的持仓，用今日 OHLC 判止损 / 止盈 / 到期（同日同时触及止损止盈 -> 保守按先止损）。今日新 promote 的不判（T+1）。
2. **再 promote**：候选用今日 OHLC 判触发，命中则按 `trigger_price` 成交，止损 / 目标缺省用 `entry×0.93 / ×1.10` 兜底。
3. **粗筛入候选**：screener 取 top N -> 逐个 analyze -> verdict ∈ bullish_verdicts 且不重复 -> `add_candidate`。

> tushare 当日日线约 17:00 后才更新，故撮合走 **cron 不走 loop**（loop 的 15:30 postmarket 会跑空）。

### 服务器 cron（推荐）

```bash
# 每交易日 18:33 跑一次自动撮合（等 tushare 当日数据出齐）
33 18 * * 1-5 cd /opt/apex && /opt/apex/.venv/bin/python -m apex.automation --auto-trade >> ~/.stock-journal/automation.log 2>&1
```

本地人工盯盘可用 `--loop` 常驻（交易日开盘前启动，收盘后 Ctrl-C 停）。

---

## 七、MCP 接入（让外部 Agent 指挥 apex）

apex 内置 MCP server（`apex/mcp_server.py`，stdio），让 Claude Code / Codex / Cursor 等外部 agent 远程读 watchlist / journal / 行情并执行交易动作。

```bash
uv pip install -e .
```

在 MCP 客户端配置中加入（`command` 必须是**绝对路径**--Claude Code 不按项目根解析相对路径，相对路径会 `ENOENT`）：

```json
{
  "mcpServers": {
    "apex-trader": {
      "command": "/Users/<you>/path/to/apex/.venv/bin/python",
      "args": ["-m", "apex.mcp_server"]
    }
  }
}
```

### 工具列表

| Tool | 说明 |
|---|---|
| `add_candidate` | 加候选（等触发买入），`trigger_price` = AI 建议入场价；同 ts_code 已有则 upsert |
| `promote_candidate` | 候选转持仓（用**实际成交价**），先验重再 archive 候选，失败不留半截状态 |
| `close_position` | 平仓，`postmortem=true` 默认串联 AI 复盘 + 校准重算 |
| `archive_entry` | 软删除（写 `status=archived_<reason>`） |
| `analyze_stock` | 跑一次 DeepSeek 个股分析（**阻塞，可能数分钟**），verdict 写 journal |
| `run_screener` | 跑今日盘后粗筛（**阻塞，开 AI 时数分钟**） |
| `run_backtest` | 信号模拟回测（看多 verdict -> T+1 开盘入场，逐笔 P&L） |

---

## 八、数据存储

全部在 `$HOME` 下（仓库内无数据），便于备份 / 迁移：

| 路径 | 说明 |
|---|---|
| `~/.stock-journal/<ts_code>.jsonl` | 每只股票一份 AI 分析日志，append-only |
| `~/.stock-journal/backtest_last.csv` | 最近一次回测结果，每次覆盖 |
| `~/.stock-journal/screener/` | 盘后粗筛报告 |
| `~/.stock-journal/automation_state.json` | 自动化状态（按天重置） |
| `~/.stock-journal/push_log.jsonl` | 持仓推送审计日志 |
| `~/.stock-watchlist/watchlist.json` | watchlist（active / candidates / archived） |

备份：

```bash
tar -czf apex-data-$(date +%Y%m%d).tar.gz ~/.stock-watchlist ~/.stock-journal
```

---

## 九、生产部署

前端 build + nginx 反代 + systemd 管 uvicorn，详见 **[`docs/deploy/README.md`](docs/deploy/README.md)**。要点：

- 后端 systemd unit 用 `--workers 1`（SSE 流式需常驻连接，多 worker 会让 stream 中断）
- nginx 必须 `proxy_buffering off` + `proxy_read_timeout 600s`（单次 AI 分析可能跑 5–10 分钟）
- CORS 上线收紧到实际域名（默认 `allow_origins=["*"]`）

```bash
# 前端构建
cd frontend
npm run build      # 产物到 dist/，由 nginx 直接服务

# 后端
sudo systemctl restart apex-backend
```

---

## 十、目录结构

```
apex/
├── apex/                 # 服务层（data/analyze/watchlist/backtest/screener/automation/...）
├── backend/              # FastAPI 薄路由（routers/ + core/ + schemas/），全挂 /api
├── frontend/             # Vite + React UI（api/ components/ routes/ hooks/）
├── docs/
│   ├── deploy/           # nginx.conf + systemd + 部署说明
│   ├── integration/      # 持仓推送 API 文档
│   └── adr/              # 架构决策记录
├── tests/                # pytest 单测
├── config.example.yaml   # 配置模板（复制为 config.yaml 后填 token）
├── requirements.txt      # Python 依赖清单
├── pyproject.toml        # uv 项目元信息 + 包发现（editable 安装用）
├── .mcp.json             # Claude Code 的 MCP server 配置（项目级，随仓库走）
└── main.py               # CLI 入口（click）
```

## 十一、关键约定

- **交易流程：candidate -> position**，不是 analysis -> position。AI verdict 常建议尚未触及的进场价，故分析不直接建仓：`add_candidate(trigger_price=AI建议进场价)` -> 实际成交后 `promote_candidate(entry_price=实际成交价)`。
- **软删除**：watchlist 不做硬删，归档项的 `status` 字段记录原因（`archived_manual` / `archived_replaced` / `archived_promoted` / `archived_dedup` / `expired`）。
- **收盘触发复盘 + 重新校准**：`close_position` 写平仓记录后，后端跑 `postmortem.run_and_patch`（AI 诊断）+ `calibration.compute()`。
- **实时 vs 日线**：`get_realtime_price`（新浪，盘中）为主，`get_latest_price`（tushare 日线收盘）为停牌 / 失败回退。

更多架构细节与设计取舍见 [`CLAUDE.md`](CLAUDE.md)（贡献者必读）与 [`docs/adr/`](docs/adr/)。

---

## 致谢与数据来源

本项目站在以下服务 / 开源项目的肩膀上：

- [tushare](https://tushare.pro/) - 日线 / 龙虎榜 / 北向等结构化数据
- [akshare](https://github.com/akfamily/akshare) - 日线 fallback
- 新浪财经 / 腾讯财经 - 实时与分时行情
- [博查 Bocha](https://open.bochaai.com/) - AI 强制网搜
- [DeepSeek](https://www.deepseek.com/) - 分析 / 复盘 / 粗筛 LLM
- [vectorbt](https://github.com/polakowo/vectorbt) - 回测引擎
- [FastAPI](https://fastapi.tiangolo.com/) / [Vite](https://vitejs.dev/) / [React](https://react.dev/) / [TanStack Query](https://tanstack.com/query) / [lightweight-charts](https://github.com/tradingview/lightweight-charts)

## 路线图

进行中与推迟的事项见 [`TODOS.md`](TODOS.md)。

## 贡献

欢迎提 Issue / PR。开发环境搭建、提交规范、PR 流程见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。请先阅读[行为准则](CODE_OF_CONDUCT.md)。

发现安全漏洞请按 [`SECURITY.md`](SECURITY.md) 私下上报，请勿直接开公开 Issue。

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。Copyright (c) 2026 wanmingyu。

---

## 完整免责声明

1. 本项目（apex）仅供学习研究、技术交流与量化方法验证，**不构成任何形式的投资建议、理财建议或交易指令**。
2. 证券投资有风险，过往回测表现不代表未来收益。本项目中的 AI 判断、回测结果、粗筛信号均可能存在错误或偏差，使用者须独立判断并自行承担一切交易后果与损失。
3. 本项目按「现状」（AS IS）提供，作者不提供任何明示或暗示的担保，不对因使用本项目而产生的任何直接或间接损失负责。
4. 使用本项目接入的任何第三方数据源 / API（tushare、akshare、新浪、腾讯、博查、DeepSeek 等）时，需自行遵守其服务条款与配额限制；因密钥泄露或违规使用造成的后果由使用者自行承担。
5. 请遵守所在地法律法规。使用者应自行判断相关行为（如自动化交易、数据爬取）的合规性。
