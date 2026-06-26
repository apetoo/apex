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

`main.py` referenced in the README does not exist; ignore those CLI examples. There is no test suite, lint config, or build step. Validate syntax with `python -c "import ast; ast.parse(open('<file>').read())"`; smoke-test behavior against `apex.watchlist` / `apex.journal` in a tempdir, or hit the backend's read-only endpoints after `uvicorn` starts.

## Configuration

`config.yaml` at repo root, loaded once via `apex.config.load()` into a module-level singleton. Tokens in YAML take priority; env vars (`TUSHARE_TOKEN`, `DEEPSEEK_API_KEY`, `BOCHA_API_KEY`) are the **fallback**, not the primary source. Path fields are tilde-expanded on load.

Default storage paths (all outside the repo, in `$HOME`):
- `~/.stock-journal/<ts_code>.jsonl` — one JSON line per AI analysis
- `~/.stock-watchlist/watchlist.json` — single file with `active_positions` / `candidates` / `archived`

## Architecture

No framework glue, no DB. `apex/` is the service layer; `backend/` is a thin HTTP routing layer over it; `frontend/` is the Vite + React UI talking to `backend/`.

### `apex/` — service layer

**`apex/data.py`** — outbound data layer. Tushare (primary) with akshare fallback for daily K-line; Sina Finance HTTP for realtime intraday (proxies stripped via custom opener); Bocha for web search. The `TOOL_FUNCTIONS` dict at the bottom auto-registers functions as tools for the AI agent — adding a new tool means adding to both `TOOLS` (in `analyze.py`) and `TOOL_FUNCTIONS` (here). Several functions return JSON **strings**; the backend unwraps them via `backend.core.response.parse_json` before responding.

**`apex/analyze.py`** — DeepSeek agent loop using OpenAI-compatible function calling. `run(ts_code)` injects formatted journal history into the user prompt (so the AI can review past calls), spins a tool-call loop until `record_verdict` is invoked, then does one final text round to capture the analyst's narrative. `max_tool_iterations` caps the loop. Verdict + features get written to the journal jsonl. Accepts an `on_progress` callback that streams trace events — the backend bridges this to SSE.

**`apex/journal.py`** — append-only jsonl reader/writer keyed by ts_code. `validate_entry` fills missing required feature keys with `None` and rejects unknown verdicts (`schemas.VERDICT_ENUM`). `merge_legacy_files()` is a one-shot migration that consolidates pre-suffix filenames (`603019.jsonl` → `603019.SH.jsonl`) and renames originals to `*.jsonl.legacy`.

**`apex/watchlist.py`** — JSON file with three sections: `active_positions` (real holdings), `candidates` (waiting for trigger), `archived` (soft-delete with `status: archived_<reason>`). Key invariant: **one position per `ts_code`** in `active_positions`. `add_position` raises `DuplicatePositionError` (carrying the existing record) on conflict — the backend maps this to HTTP 409 with `{ts_code, existing}` in the body so the client can offer replace-or-cancel.

**`apex/backtest.py`** — vectorbt-based. Each bullish journal entry becomes its own `Portfolio.from_signals` over an N-day window starting at `entry_date`, using `price_advice.stop_loss/target` as fractional SL/TP anchored to the actual fill price. One-portfolio-per-entry to avoid signal collision when the same stock has multiple verdicts.

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
    ├── backtest.py  screener.py  calibration.py
    └── postmortem.py  chat.py
```

Design rules:
- **No `services/` layer.** `apex.*` is the service layer; a forwarding layer would be pure overhead. Routers do: parse request → `normalize_ts_code` → call `apex.*` → return JSON.
- **Exception mapping** (`core/errors.py`) so routers have no try/except boilerplate: `DuplicatePositionError→409`, `PositionNotFoundError→404`, `AnalysisError→502`, `PostmortemError→500`, `ValueError→400`.
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
│   ├── base/               # shadcn-style Card / Button / ChatPanel foundation
│   └── a-share/            # A-share primitives: PriceTag / VerdictTag / PositionCard / CandidateCard / MarketIndexBar
├── routes/                 # 5 pages: overview / watchlist / analyze / backtest / screener
├── hooks/                  # useSSE (hand-written, no MSW) + useChatContext (single-injection hash tracking)
├── lib/utils.ts            # cn / directionClass / formatPrice / formatDelta / formatPercent / formatRatio
└── types/verdict.ts        # VERDICT_COLOR TS const
```

Vite proxy (`/api` → 127.0.0.1:8000) for dev. Production: `VITE_USE_MOCK=0 npm run build` + nginx from `docs/deploy/nginx.conf`.

Routes:
- `/` Overview — market index bar (ED13 single source) + positions + chat trigger
- `/watchlist` — active / candidates / archived tabs with inline add form
- `/analyze` — SSE trace stream + verdict card + journal history
- `/backtest` — per-signal bar chart + stats table + realized closed trades
- `/screener` — strategy weight sliders (localStorage) + report with regime/by_strategy/top_scored

SSE events from backend (`backend/core/streaming.py`): `trace` / `progress` / `chunk` / `done` / `error`. The frontend's `useSSE` is hand-written (no `@microsoft/fetch-event-source` — default auto-reconnect re-runs DeepSeek + double-writes journal). All SSE paths support a `fetchFn` injection for dev mock (no MSW).

## Critical conventions

**ts_code normalization.** Everything below the UI/API assumes the suffixed form (`002050.SZ`, `603019.SH`, `838810.BJ`). Always pass user input through `data.normalize_ts_code()` before storing, looking up history, or calling tushare. The first digit determines exchange: 6→SH, 0/3→SZ, 4/8→BJ. The backend applies this at every endpoint boundary.

**Trading flow: candidate → position, not analysis → position.** AI verdicts often suggest entry prices that aren't yet met, so analysis does not directly create a position. The flow is:
- `add_candidate()` with `trigger_price` = AI's suggested entry, optional `stop_advice` / `target_advice` carried forward
- direct `add_position()` only for already-filled trades
- `promote_candidate(ts_code, entry_price, stop_loss, target)` — once filled, archives the candidate as `archived_promoted` and creates the active position with **actual** fill values (not the trigger price). Validates no duplicate position **before** archiving the candidate so failures leave state intact.

**Soft delete via `archive_entry`.** Nothing is hard-deleted from the watchlist. The `status` field on archived items records the reason: `archived_manual`, `archived_replaced`, `archived_promoted`, `archived_dedup`, `expired`.

**One-shot migration.** `migrate_and_backfill()` normalizes legacy ts_codes, backfills missing names via tushare, merges legacy unsuffixed journal files, and dedups duplicate active positions. Runs once per session; in the backend it runs in `lifespan` at startup. Add new one-shot fixes here rather than scattering migration logic.

**Realtime vs daily price.** `get_realtime_price` (Sina, intraday) is the primary; `get_latest_price` (tushare daily close) is the fallback used when realtime returns None (suspended / API failure). `GET /api/market/prices` replicates this priority; `GET /api/market/prices/realtime` and `/daily` expose each source directly.

**Closing a position triggers postmortem + recalibration.** `close_position` writes a closed record; the backend's `POST /api/watchlist/close` then runs `postmortem.run_and_patch` (AI diagnosis) and `calibration.compute()` when `postmortem=true` (default). `POST /api/postmortem/run` re-runs diagnosis on an existing closed record by `closed_at`.

**Adding an AI tool** requires three coordinated edits: (1) implement the function in `apex/data.py` returning a JSON string, (2) register it in `data.TOOL_FUNCTIONS`, (3) declare its schema in `analyze.TOOLS`. The agent dispatches by name through `_dispatch_tool` which only looks at `TOOL_FUNCTIONS`.
