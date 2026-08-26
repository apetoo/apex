# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Personal A-share (Chinese stock market) trading loop: DeepSeek AI analysis + watchlist tracking + vectorbt backtesting. Single-user tool, file-based storage.

The project is decoupled: FastAPI backend (`backend/`) + Vite/React frontend (`frontend/`). Streamlit UI retired in commit `675807e`.

## Run

```bash
pip install -r requirements.txt                       # deps (Python 3.12+)

# Backend (FastAPI)
uvicorn backend.main:app --reload --port 8000         # http://localhost:8000/docs

# Frontend (Vite + React)
cd frontend && npm install && npm run dev             # http://localhost:5173
```

`main.py` referenced in the README does not exist; ignore those CLI examples. No lint config or build step for Python (frontend has `npm run build`). Test suite: `tests/` (pytest, configured via `pyproject.toml`, `pytest>=8.0.0` in requirements) — run `.venv/bin/python -m pytest`. Frontend: `cd frontend && npx vitest run` + `npx tsc --noEmit`. For quick checks, validate syntax with `python -c "import ast; ast.parse(open('<file>').read())"`; smoke-test behavior against `apex.watchlist` / `apex.journal` in a tempdir, or hit the backend's read-only endpoints after `uvicorn` starts.

## Configuration

`config.yaml` at repo root, loaded once via `apex.config.load()` into a module-level singleton. Tokens in YAML take priority; env vars (`TUSHARE_TOKEN`, `DEEPSEEK_API_KEY`, `BOCHA_API_KEY`) are the **fallback**, not the primary source. Path fields are tilde-expanded on load.

Default storage paths (all outside the repo, in `$HOME`):
- `~/.stock-journal/<ts_code>.jsonl` — one JSON line per AI analysis
- `~/.stock-watchlist/watchlist.json` — single file with `active_positions` / `candidates` / `archived`

## Architecture

No DB. `apex/` is the service layer; `backend/` is a thin HTTP routing layer over it; `frontend/` is the Vite + React UI talking to `backend/`. Stock analysis uses LangGraph as its single orchestration path.

### `apex/` — service layer

**`apex/data.py`** — outbound data layer. Tushare (primary) with akshare fallback for daily K-line; Sina Finance HTTP for realtime intraday (proxies stripped via custom opener); Bocha for web search. The `TOOL_FUNCTIONS` dict at the bottom auto-registers functions as tools for the AI agent — adding a new tool means adding to both `TOOLS` (in `analyze.py`) and `TOOL_FUNCTIONS` (here). Several functions return JSON **strings**; the backend unwraps them via `backend.core.response.parse_json` before responding.

**`apex/analyze.py`** — DeepSeek stock-analysis agent using OpenAI-compatible function calling. `run(ts_code)` injects context, then LangGraph performs an authoritative risk scan, gap-driven tool research, evidence convergence, draft, independent review, and either completion or `insufficient_evidence`. Dynamic time/call/round budgets cap research. Accepts an `on_progress` callback that streams trace events — the backend bridges this to SSE.

**`apex/journal.py`** — append-only jsonl reader/writer keyed by ts_code. `validate_entry` fills missing required feature keys with `None` and rejects unknown verdicts (`schemas.VERDICT_ENUM`). `merge_legacy_files()` is a one-shot migration that consolidates pre-suffix filenames (`603019.jsonl` → `603019.SH.jsonl`) and renames originals to `*.jsonl.legacy`.

**`apex/watchlist.py`** — JSON file with three sections: `active_positions` (real holdings), `candidates` (waiting for trigger), `archived` (soft-delete with `status: archived_<reason>`). Key invariant: **one position per `ts_code`** in `active_positions`. `add_position` raises `DuplicatePositionError` (carrying the existing record) on conflict — the backend maps this to HTTP 409 with `{ts_code, existing}` in the body so the client can offer replace-or-cancel.

**`apex/backtest.py`** — signal backtests use one deterministic daily-bar execution path as the source of truth for exit price, date, reason, and P&L. A bullish journal entry fills at T+1 open; A-share T+1 forbids selling on the fill day; gaps fill at the next open, intraday stop/target collisions resolve stop-first, and locked limit-down exits defer until a sellable bar. Stop/target advice is enabled only when the finite pair satisfies `stop < actual fill < target`. Results carry `completed/pending/data_truncated/unfillable/no_fill_data`; only completed and conservatively closed historical truncations enter official win-rate statistics. `vectorbt` remains in use for the shared-cash portfolio equity view, where open positions are marked to market but excluded from closed-trade win rate.

**`apex/technical.py` + `apex/chan.py`** — the `/chan` technical-analysis path. `technical.fetch_raw_bars()` fetches unadjusted completed D/W/30/60-minute bars without touching the screener cache; `chan.get_structure()` runs czsc and serializes bars, strokes, centers, approximate buy/sell points, and the current summary. Data-source failures map to 502 and an unavailable czsc runtime maps to 501.

### `backend/` — FastAPI HTTP layer (thin routing over `apex/`)

```
backend/
├── main.py            # app factory + lifespan + CORS + route registration + /api/health
├── core/
│   ├── lifespan.py    # startup: load config + migrate_and_backfill
│   ├── errors.py      # apex exceptions → HTTP status codes (global handlers)
│   ├── response.py    # parse_json (unwrap apex's JSON strings) + DataFrame→records
│   └── streaming.py   # SSE bridge: callback-style (analyze/screener) + generator-style (chat)
├── schemas/           # Pydantic request models (responses reuse apex's raw dicts)
└── routers/           # one file per domain, all mounted under /api
    ├── market.py  watchlist.py  account.py  analyze.py
    ├── backtest.py  screener.py  calibration.py  chan.py
    └── postmortem.py  chat.py
```

Design rules:
- **No `services/` layer.** `apex.*` is the service layer; a forwarding layer would be pure overhead. Routers do: parse request → `normalize_ts_code` → call `apex.*` → return JSON.
- **Exception mapping** (`core/errors.py`) so routers have no try/except boilerplate: `DuplicatePositionError→409`, `PositionNotFoundError→404`, `AnalysisError/DataFetchError→502`, `ChanUnavailableError→501`, `PostmortemError→500`, `ValueError→400`.
- **Streaming.** `analyze.run` / `screener.run` are blocking calls that take an `on_progress` callback — bridged via a worker thread + `queue.Queue` to an async SSE generator. `llm.chat_stream` is already a sync generator — wrapped with `run_in_executor`. SSE events: `trace` / `progress` / `chunk` / `done` / `error`.
- **Every endpoint that takes a ts_code normalizes it first** via `data.normalize_ts_code()`.

### `frontend/` — Vite + React UI

雪球风 (Xueqiu) — light background, card-based, restrained red/green, monospace numbers. **A-share convention: 红涨绿跌** (red up, green down) — opposite of US markets.

Stack: Vite 8 + React 19 + TypeScript 6 + Tailwind v4 + TanStack Query v5 + react-router v6 + lightweight-charts (candlestick) + lucide-react. Tests: Vitest + @testing-library/react. 62 unit tests covering a-share primitives, useSSE, useChatContext, query keys, utils, mutations.

```
frontend/src/
├── api/                    # REST client + per-domain modules
│   ├── client.ts           # fetch wrapper with ApiError class (409 from watchlist carries existing record)
│   ├── query-keys.ts       # ED3 invalidation matrix (SSOT for all keys)
│   ├── mutations.ts        # 6 react-query mutations (addCandidate/addPosition/replace/promote/close/archive)
│   ├── market.ts watchlist.ts account.ts analyze.ts chat.ts screener.ts backtest.ts
├── components/
│   ├── base/               # shadcn-style Card / Button / ChatPanel / Markdown / Drawer foundation
│   └── a-share/            # A-share primitives: PriceTag / VerdictTag / PositionCard / CandidateCard / MarketIndexBar / VerdictDetailCard
├── routes/                 # pages: overview / watchlist / analyze / journal / backtest / screener / chan
├── hooks/                  # useSSE (hand-written, no MSW) + useChatContext (single-injection hash tracking)
├── lib/utils.ts            # cn / directionClass / formatPrice / formatDelta / formatPercent / formatRatio
└── types/verdict.ts        # VERDICT_COLOR TS const
```

Vite proxy (`/api` → 127.0.0.1:8000) for dev. Production: `npm run build` + nginx from `docs/deploy/nginx.conf`.

Routes:
- `/` Overview — market index bar (ED13 single source) + positions + chat trigger
- `/watchlist` — active / candidates / archived tabs with inline add form
- `/analyze` — SSE trace stream + verdict card + per-stock journal history (概要)
- `/journal` — cross-stock full history list + search + click-row → `<Drawer>` with `<VerdictDetailCard>` (完整结果)
- `/backtest` — per-signal bar chart + stats table + realized closed trades
- `/screener` — strategy weight sliders (localStorage) + report with regime/by_strategy/top_scored
- `/chan` — unadjusted completed K-lines with czsc strokes, centers, approximate buy/sell points, and D/W/30/60-minute switching

SSE events from backend (`backend/core/streaming.py`): `trace` / `progress` / `chunk` / `done` / `error`. The frontend's `useSSE` is hand-written (no `@microsoft/fetch-event-source` — default auto-reconnect re-runs DeepSeek + double-writes journal). All SSE paths support a `fetchFn` injection (for unit tests; no MSW).

## Critical conventions

**ts_code normalization.** Everything below the UI/API assumes the suffixed form (`002050.SZ`, `603019.SH`, `920185.BJ`). Always pass user input through `data.normalize_ts_code()` before storing, looking up history, or calling tushare. The first digit determines exchange: 6/5→SH, 0/3/1→SZ, 4/8/9→BJ (9 is the migrated 920xxx Beijing segment). The backend applies this at every endpoint boundary; frontend `normalizeTsCode` mirrors the same mapping.

**Trading flow: candidate → position, not analysis → position.** AI verdicts often suggest entry prices that aren't yet met, so analysis does not directly create a position. The flow is:
- `add_candidate()` with `trigger_price` = AI's suggested entry, optional `stop_advice` / `target_advice` carried forward
- direct `add_position()` only for already-filled trades
- `promote_candidate(ts_code, entry_price, stop_loss, target)` — once filled, archives the candidate as `archived_promoted` and creates the active position with **actual** fill values (not the trigger price). Validates no duplicate position **before** archiving the candidate so failures leave state intact.

**Chan decision contract and candidate flow.** `GET /api/chan/{ts_code}` keeps its existing structure fields and additively returns `decision`: `bias` (`long` / `neutral` / `risk`), `setup`, `state` (`watching` / `pending` / `confirmed` / `invalid`), signal age, confirmation/invalidation levels, candidate trigger/range, eligibility/reason, and explainable `basis`. Compute every decision only from **completed bars**: a buy signal confirms only after a later completed close exceeds the signal-bar high, and it invalidates when the current completed close falls below the signal price. `risk` bias and `invalid` state mean no current long action; `watching` means no actionable structure, while `pending` awaits confirmation. Do not submit stale signals (older than 10 bars), invalid structures, or any decision with `candidate_eligible=false`. For an upward confirmed-center breakout, do not chase: create the retrace window `[zg, zg × 1.01]` with `trigger_direction="below"`; its invalidation is `zd`. Eligible decisions open an editable candidate form and require an explicit user confirmation; submit it with `strategy="chan"`, preserve the selected trigger/stop values, and leave the target unset. This is a single-timeframe approximation, not lower-timeframe confirmation: display that limitation, and keep the repaint warning visible because an unfinished last stroke can change with later bars.

**Soft delete via `archive_entry`.** Nothing is hard-deleted from the watchlist. The `status` field on archived items records the reason: `archived_manual`, `archived_replaced`, `archived_promoted`, `archived_dedup`, `expired`.

**One-shot migration.** `migrate_and_backfill()` normalizes legacy ts_codes, backfills missing names via tushare, merges legacy unsuffixed journal files, and dedups duplicate active positions. Runs once per session; in the backend it runs in `lifespan` at startup. Add new one-shot fixes here rather than scattering migration logic.

**Realtime vs daily price.** `get_realtime_price` (Sina, intraday) is the primary; `get_latest_price` (tushare daily close) is the fallback used when realtime returns None (suspended / API failure). `GET /api/market/prices` replicates this priority; `GET /api/market/prices/realtime` and `/daily` expose each source directly.

**Closing a position triggers postmortem + recalibration.** `close_position` writes a closed record; the backend's `POST /api/watchlist/close` then runs `postmortem.run_and_patch` (AI diagnosis) and `calibration.compute()` when `postmortem=true` (default). `POST /api/postmortem/run` re-runs diagnosis on an existing closed record by `closed_at`.

**Adding an external-data AI tool** requires three coordinated edits: (1) implement the function in `apex/data.py` returning a JSON string, (2) register it in `data.TOOL_FUNCTIONS`, (3) declare its schema in `analyze.TOOLS`. The graph dispatches external tools through `_dispatch_tool`. Control pseudo-tools such as `submit_research_state` are declared in `analyze.TOOLS` and handled directly by the graph; they must not be registered as outbound data functions.

**SSE data contract (cross前后端).** `sse_starlette` 3.x 的 `EventSourceResponse` 对 `data` 直接 `str()` —— dict 会变 Python repr（单引号），前端 `JSON.parse` 必失败。所以 `backend/core/streaming.py` 的 `_sse()` helper 把 data 先 `json.dumps(ensure_ascii=False, default=str)` 成字符串再 yield；**新增 SSE 事件必须走 `_sse()`，不要直接 `yield {"event":..., "data": <dict>}`**。另一坑：sse_starlette 行尾是 `\r\n`（事件间 `\r\n\r\n`），而单测用例用 `\n` —— 前端 `useSSE.parseSSEChunk` 必须用 `split(/\r?\n\r?\n/)` 兼容两者，否则真后端事件全堆 buffer、单测却全过（盲区）。

**analyze verdict 字段形状.** `analyze.run` 返回的 entry 是**嵌套**结构：`analysis_status: completed|insufficient_evidence`、`price_advice: {entry, stop_loss, target, position_size_pct}`、`evidence: EvidenceItem[]`、`unknowns`、`research_summary`、`analysis_text`（非 `note`）、`calibrated_confidence` / `calibration_explanation`、`analyzed_at`（非 `date`）。旧 journal 的 `evidence: string[]` 仍可读。**写前端字段前以 `apex/analyze.py:run` 的返回 dict 为准**。`tool_result` trace 事件同理带的是 `summary`(dict) + `raw`(原始 JSON str)，不是 `result`。共享渲染走 `components/a-share/VerdictDetailCard`，AI 叙述走 `components/base/Markdown`（AI 输出含 markdown，别用 `whitespace-pre-wrap` 纯文本）。

**分时不是 AI 工具，且原始 bars 不落盘.** `get_intraday_snapshot` / `get_intraday_bars` 不在 `data.TOOL_FUNCTIONS` —— 别把它们注册成 AI 工具（启动时 `_format_intraday_block` 强制注入特征到 prompt，设计上不让 AI 决定调不调）。分时原始 1 分钟 bars 在 Python 压成特征后即弃，**不进 prompt、不写 trace.jsonl**；trace 里只有日线 raw（`get_daily_price` 是工具，落 `tool_result.raw`）。所以前端分时图只能实时调 `/api/market/intraday/{ts_code}/bars`，**无法从 trace 回放历史分时**。

**web_search 是查漏补缺，不做类别打卡.** 每次分析自动执行窄范围权威风险扫描；其余搜索由证据缺口驱动。搜索失败、空结果、串票或 Tier 3 线索不算覆盖，重大 Tier 2 事实需要 Tier 1 或第二独立来源佐证（90 天内的新鲜事实；更陈旧的单源事实不硬阻断，交由独立复核按重要性把关）。最终提交由 evidence controller 和独立复核共同门控。

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
