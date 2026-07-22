# AGENTS.md

给 AI 编程助手（Codex 等）的入门指引。**完整的架构、约定与陷阱见 [`CLAUDE.md`](CLAUDE.md)，它是本仓库的单一事实来源（SSOT），以下仅摘要。**

## Project

个人 A 股交易闭环系统：DeepSeek AI 分析 + 持仓追踪 + vectorbt 回测 + 盘后粗筛 + 模拟实盘自动化。单用户工具，文件存储，无 DB。

解耦三层：`apex/`（服务层，无框架）+ `backend/`（FastAPI 薄路由，挂 `/api`）+ `frontend/`（Vite + React UI）。

## Run

```bash
# 后端
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000     # http://localhost:8000/docs

# 前端
cd frontend && npm install && npm run dev          # http://localhost:5173
```

CLI 入口 `main.py`（基于 click）。详见 README「CLI 命令」章节。

> 注：早期的 Streamlit UI（`app.py`）已退役，不要参考任何关于 `app.py` / `st.session_state` 的旧描述。

## Configuration

`config.yaml`（仓库根，**已被 .gitignore 忽略**）。首次使用复制模板：

```bash
cp config.example.yaml config.yaml   # 填入 tushare / deepseek / bocha token
```

由 `apex.config.load()` 读一次，缓存为模块单例。YAML 值优先；留空回退环境变量（`TUSHARE_TOKEN` / `DEEPSEEK_API_KEY` / `BOCHA_API_KEY` / `MX_APIKEY`）。

## 验证

- 后端语法检查：`python -c "import ast; ast.parse(open('apex/<file>').read())"`
- 后端测试：`pytest -q`（部分依赖 tushare token / 网络）
- 前端测试：`cd frontend && npm test`（vitest）

## 关键约定（详见 CLAUDE.md）

- **ts_code 规范化**：所有入口先过 `apex.data.normalize_ts_code()`（6->SH、0/3->SZ、4/8->BJ）。
- **A 股配色**：红涨绿跌（与美股相反）。
- **交易流程**：candidate -> position，分析不直接建仓。
- **软删除**：watchlist 不硬删，归档项用 `status` 字段记原因。
- **SSE 契约**：新事件走 `backend/core/streaming.py` 的 `_sse()` helper。
- **新增 AI 工具**三处协同：`apex/data.py` 实现 + `data.TOOL_FUNCTIONS` 注册 + `analyze.TOOLS` 声明 schema。

改任何东西前，先读 [`CLAUDE.md`](CLAUDE.md)。
