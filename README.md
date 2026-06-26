# apex — A股 AI 交易闭环系统

个人 A 股量化工具：DeepSeek AI 分析 + 持仓追踪 + 回测。

## 安装

```bash
pip install -r requirements.txt
```

## 配置

编辑 `config.yaml`，填入 token 和 API key：

```yaml
tushare:
  token: "你的 tushare token"   # tushare.pro 注册获取

deepseek:
  api_key: "sk-..."             # platform.deepseek.com 获取
  model: "deepseek-chat"        # deepseek-chat = V3，deepseek-reasoner = R1
```

路径配置（默认不用改）：

```yaml
paths:
  journal_dir: "~/.stock-journal"           # AI 分析日志
  watchlist_file: "~/.stock-watchlist/watchlist.json"  # 持仓 & 候选
  prompt_file: "~/.claude/skills/stock-analyze/prompts/expert-persona.md"
```

## 启动 Web UI

后端 (FastAPI):

```bash
uvicorn backend.main:app --reload --port 8000
```

前端 (Vite + React, 雪球风):

```bash
cd frontend
npm install        # 首次
npm run dev        # http://localhost:5173
```

dev 模式默认走 mock 数据(后端不在也能跑可视化),要切真后端:

```bash
VITE_USE_MOCK=0 npm run dev
```

| 路由 | 功能 |
|---|---|
| `/` | 概览(联调/市场温度) |
| `/watchlist` | 持仓 + 候选(价格/止损目标/盈亏/触发) |
| `/analyze` | 个股分析(SSE trace 流式 + verdict 注入 chat) |
| `/backtest` | 逐笔 P&L + 统计(柱状图替代净值曲线) |
| `/screener` | 多策略粗筛(权重可调 + SSE 进度 + AI 综合) |
| 右下角 | 全局 chat(单次注入当前页上下文) |

## CLI 命令

```bash
# 分析单只股票
python main.py analyze 002050.SZ

# 每日晨报（检查 watchlist 触发，自动重分析）
python main.py daily

# 回测所有历史判断
python main.py backtest

# 回测特定股票，指定持仓天数
python main.py backtest --ts-code 002050.SZ --days 5

# 查看 watchlist
python main.py watchlist list

# 添加持仓（进场 44，止损 43.2，目标 48，现价跌破 44.75 时触发重分析）
python main.py watchlist add 002050.SZ --entry 44 --stop 43.2 --target 48 --trigger 44.75 --direction below --name 三花智控

# 添加候选（涨过 38.5 时触发）
python main.py watchlist watch 000034.SZ --trigger 38.5 --direction above --name 神州数码
```

## 数据说明

- **分析日志**：`~/.stock-journal/<代码>.jsonl`，每次分析追加一行
- **Watchlist**：`~/.stock-watchlist/watchlist.json`，持仓 + 候选 + 已归档
- **回测结果**：`~/.stock-journal/backtest_last.csv`，每次覆盖

## 每日自动晨报（可选 cron）

```bash
# 每天早上 9:15 跑晨报
15 9 * * 1-5 cd /Users/wanmingyu/workspace/my/apex && python main.py daily >> ~/.stock-journal/daily.log 2>&1
```
