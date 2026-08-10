# 贡献指南

感谢你对 apex 的关注！本文档说明如何搭建开发环境、跑测试、提交代码。

## 开发环境

```bash
# 后端
git clone https://github.com/apetoo/apex.git
cd apex
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt
cp config.example.yaml config.yaml   # 填入你自己的 token（config.yaml 已被 gitignore）

# 前端
cd frontend && npm install
```

> ⚠️ **切勿提交 `config.yaml`**。它含真实 API token，已被 `.gitignore` 忽略。如需新增配置项，请同步更新 `config.example.yaml`。

## 跑测试

后端（pytest）：

```bash
pytest -q
```

> 部分后端测试依赖 tushare 数据 / 网络，离线可能失败。纯离线兜底可用语法检查：
> `python -c "import ast, pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('.').rglob('*.py') if 'venv' not in str(p)]"`

前端（Vitest）：

```bash
cd frontend
npm test            # watch 模式
npm run build       # 构建检查
```

## 代码风格与约定

本项目无 lint 配置，但有若干强约定，提交前请确保不破坏：

- **ts_code 规范化**：所有入口先用 `apex.data.normalize_ts_code()` 规范化（带后缀：6/5->SH、0/3/1->SZ、4/8/9->BJ；9 为 920xxx 北交所新码段）。
- **A 股配色**：红涨绿跌（与美股相反），前端走 `lib/utils.ts` 的 `directionClass` / `formatDelta`。
- **SSE 数据契约**：新增 SSE 事件必须走 `backend/core/streaming.py` 的 `_sse()` helper（先 `json.dumps` 再 yield），不要直接 yield dict。详见 `CLAUDE.md` 的 SSE 章节。
- **新增 AI 工具**需三处协同：`apex/data.py` 实现（返回 JSON 字符串）+ `data.TOOL_FUNCTIONS` 注册 + `analyze.TOOLS` 声明 schema。
- **软删除**：watchlist 不做硬删，归档项用 `status` 字段记录原因。

更完整的架构与设计取舍见 [`CLAUDE.md`](CLAUDE.md)（贡献者必读）与 [`docs/adr/`](docs/adr/)。

## 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) + 中文描述：

```
<type>(<scope>): <简要描述>

<可选正文>
```

常用 type：`feat` / `fix` / `docs` / `refactor` / `chore` / `test` / `perf`。scope 用模块名（如 `crowd`、`backtest`、`analyze`、`frontend`）。

示例：

```
feat(screener): 北向信号权重可配置
fix(backtest): 修复生存者偏差导致的胜率高估
docs: 补充 SSE 数据契约说明
```

## 分支与 PR

1. 从 `main` 切功能分支：`feat/xxx` / `fix/xxx`。
2. 提交前确保前端测试通过、后端语法检查通过。
3. PR 描述说明动机、改动点、是否破坏既有行为；如有 UI 改动附截图。
4. 涉及行为变更的，更新 `CLAUDE.md` / README 相关章节。

## 报告问题

- Bug / 功能建议：开 [Issue](../../issues)，附复现步骤与 `ts_code` / 配置（**脱敏**，勿贴 token）。
- 安全漏洞：按 [`SECURITY.md`](SECURITY.md) 私下上报，**不要**开公开 Issue。

## 行为准则

参与本项目即代表同意遵守 [行为准则](CODE_OF_CONDUCT.md)。请保持友善、尊重。
