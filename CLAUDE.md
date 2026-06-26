# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Personal A-share (Chinese stock market) trading loop: DeepSeek AI analysis + watchlist tracking + vectorbt backtesting. Single-user tool, file-based storage.

The project is mid-migration from a Streamlit monolith (`app.py`) to a decoupled frontend/backend. Both entry points currently coexist; new work targets the FastAPI backend + a separate frontend.

## Run

```bash
pip install -r requirements.txt                       # deps (Python 3.12+)

# Backend (FastAPI) — the going-forward API layer
uvicorn backend.main:app --reload --port 8000         # http://localhost:8000/docs

# Legacy Streamlit UI — still works, reads/writes the same files as the backend
streamlit run app.py                                   # http://localhost:8501
```

`main.py` referenced in the README does not exist; ignore those CLI examples. There is no test suite, lint config, or build step. Validate syntax with `python -c "import ast; ast.parse(open('<file>').read())"`; smoke-test behavior against `apex.watchlist` / `apex.journal` in a tempdir, or hit the backend's read-only endpoints after `uvicorn` starts.

## Configuration

`config.yaml` at repo root, loaded once via `apex.config.load()` into a module-level singleton. Tokens in YAML take priority; env vars (`TUSHARE_TOKEN`, `DEEPSEEK_API_KEY`, `BOCHA_API_KEY`) are the **fallback**, not the primary source. Path fields are tilde-expanded on load.

Default storage paths (all outside the repo, in `$HOME`):
- `~/.stock-journal/<ts_code>.jsonl` — one JSON line per AI analysis
- `~/.stock-watchlist/watchlist.json` — single file with `active_positions` / `candidates` / `archived`

## Architecture

No framework glue, no DB. `apex/` is the service layer (used by both `app.py` and `backend/`); `backend/` is a thin HTTP routing layer over it.

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

### `app.py` — legacy Streamlit UI

Single-file Streamlit app, four tabs (Watchlist / Analyze / Backtest / Screener) plus an AI chat sidebar. Still functional and shares the same on-disk state as the backend. Being replaced by the decoupled frontend; avoid adding new features here.

## Critical conventions

**ts_code normalization.** Everything below the UI/API assumes the suffixed form (`002050.SZ`, `603019.SH`, `838810.BJ`). Always pass user input through `data.normalize_ts_code()` before storing, looking up history, or calling tushare. The first digit determines exchange: 6→SH, 0/3→SZ, 4/8→BJ. The backend applies this at every endpoint boundary.

**Trading flow: candidate → position, not analysis → position.** AI verdicts often suggest entry prices that aren't yet met, so analysis does not directly create a position. The flow is:
- `add_candidate()` with `trigger_price` = AI's suggested entry, optional `stop_advice` / `target_advice` carried forward
- direct `add_position()` only for already-filled trades
- `promote_candidate(ts_code, entry_price, stop_loss, target)` — once filled, archives the candidate as `archived_promoted` and creates the active position with **actual** fill values (not the trigger price). Validates no duplicate position **before** archiving the candidate so failures leave state intact.

**Soft delete via `archive_entry`.** Nothing is hard-deleted from the watchlist. The `status` field on archived items records the reason: `archived_manual`, `archived_replaced`, `archived_promoted`, `archived_dedup`, `expired`.

**One-shot migration.** `migrate_and_backfill()` normalizes legacy ts_codes, backfills missing names via tushare, merges legacy unsuffixed journal files, and dedups duplicate active positions. In Streamlit it runs once per session; in the backend it runs in `lifespan` at startup. Add new one-shot fixes here rather than scattering migration logic.

**Realtime vs daily price.** `get_realtime_price` (Sina, intraday) is the primary; `get_latest_price` (tushare daily close) is the fallback used when realtime returns None (suspended / API failure). `GET /api/market/prices` replicates this priority; `GET /api/market/prices/realtime` and `/daily` expose each source directly.

**Closing a position triggers postmortem + recalibration.** `close_position` writes a closed record; the backend's `POST /api/watchlist/close` then runs `postmortem.run_and_patch` (AI diagnosis) and `calibration.compute()` when `postmortem=true` (default), matching the Streamlit flow. `POST /api/postmortem/run` re-runs diagnosis on an existing closed record by `closed_at`.

**Adding an AI tool** requires three coordinated edits: (1) implement the function in `apex/data.py` returning a JSON string, (2) register it in `data.TOOL_FUNCTIONS`, (3) declare its schema in `analyze.TOOLS`. The agent dispatches by name through `_dispatch_tool` which only looks at `TOOL_FUNCTIONS`.
