## 改动说明

<!-- 这个 PR 做了什么？为什么？ -->

## 动机 / 背景

<!-- 关联的 Issue（如 #123），或要解决的痛点 -->

## 改动类型

- [ ] 新功能 (feat)
- [ ] Bug 修复 (fix)
- [ ] 文档 (docs)
- [ ] 重构 (refactor)
- [ ] 测试 (test)
- [ ] 构建 / CI (chore)

## 自检清单

- [ ] 前端：`cd frontend && npm run build && npm test` 通过
- [ ] 后端：语法检查通过（`python -c "import ast; ..."` 或 `pytest -q`）
- [ ] 未提交 `config.yaml` 或任何真实 token
- [ ] 涉及行为变更的，已更新 `CLAUDE.md` / `README.md` 相关章节
- [ ] 涉及新 AI 工具的，已三处协同（`data.py` + `TOOL_FUNCTIONS` + `analyze.TOOLS`）
- [ ] 涉及 SSE 新事件的，走 `backend/core/streaming.py` 的 `_sse()` helper

## 截图 / 测试输出（如适用）

<!-- UI 改动请附截图；行为改动可附测试输出 -->
