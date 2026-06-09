# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project

Personal A-share (Chinese stock market) trading loop: DeepSeek AI analysis + watchlist tracking + vectorbt backtesting. Single-user tool, file-based storage.

## Run

```bash
pip install -r requirements.txt          # deps (Python 3.12+)
streamlit run app.py                      # primary entry point — opens http://localhost:8501
```

The README references `python main.py ...` CLI commands, but **`main.py` does not exist**. Treat those as stale; the only working entry point is `app.py`. Modules under `apex/` (e.g. `apex.briefing.run()`) are importable but have no CLI wrapper.

There is no test suite, lint config, or build step. Validation is done via `python -c "import ast; ast.parse(open('apex/<file>.py').read())"` for syntax and tempdir-based smoke tests against `apex.watchlist` / `apex.journal` for behavior.

## Configuration

`config.yaml` at repo root, loaded once via `apex.config.load()` into a module-level singleton. Tokens in YAML take priority; env vars (`TUSHARE_TOKEN`, `DEEPSEEK_API_KEY`, `BOCHA_API_KEY`) are the **fallback**, not the primary source. Path fields are tilde-expanded on load.

Default storage paths (all outside the repo, in `$HOME`):
- `~/.stock-journal/<ts_code>.jsonl` — one JSON line per AI analysis
- `~/.stock-watchlist/watchlist.json` — single file with `active_positions` / `candidates` / `archived`

## Architecture

Three layers, no framework, no DB. Read these together to make sense of any change:

**`apex/data.py`** — outbound data layer. Tushare (primary) with akshare fallback for daily K-line; Sina Finance HTTP for realtime intraday (proxies stripped via custom opener); Bocha for web search. The `TOOL_FUNCTIONS` dict at the bottom auto-registers functions as tools for the AI agent — adding a new tool means adding to both `TOOLS` (in `analyze.py`) and `TOOL_FUNCTIONS` (here).

**`apex/analyze.py`** — DeepSeek agent loop using OpenAI-compatible function calling. `run(ts_code)` injects formatted journal history into the user prompt (so the AI can review past calls), spins a tool-call loop until `record_verdict` is invoked, then does one final text round to capture the analyst's narrative. `max_tool_iterations` caps the loop. Verdict + features get written to the journal jsonl.

**`apex/journal.py`** — append-only jsonl reader/writer keyed by ts_code. `validate_entry` fills missing required feature keys with `None` and rejects unknown verdicts (`schemas.VERDICT_ENUM`). `merge_legacy_files()` is a one-shot migration that consolidates pre-suffix filenames (`603019.jsonl` → `603019.SH.jsonl`) and renames originals to `*.jsonl.legacy`.

**`apex/watchlist.py`** — JSON file with three sections: `active_positions` (real holdings), `candidates` (waiting for trigger), `archived` (soft-delete with `status: archived_<reason>`). Key invariant: **one position per `ts_code`** in `active_positions`. `add_position` raises `DuplicatePositionError` (carrying the existing record) on conflict; the UI catches this and offers a replace-or-cancel decision via `st.session_state["_dup_pos"]`.

**`apex/backtest.py`** — vectorbt-based. Each bullish journal entry becomes its own `Portfolio.from_signals` over an N-day window starting at `entry_date`, using `price_advice.stop_loss/target` as fractional SL/TP anchored to the actual fill price. One-portfolio-per-entry to avoid signal collision when the same stock has multiple verdicts.

**`app.py`** — Streamlit UI, three tabs (Watchlist / Analyze / Backtest). Single file (~480 lines).

## Critical conventions

**ts_code normalization.** Everything below the UI assumes the suffixed form (`002050.SZ`, `603019.SH`, `838810.BJ`). Always pass user input through `data.normalize_ts_code()` before storing, looking up history, or calling tushare. The first digit determines exchange: 6→SH, 0/3→SZ, 4/8→BJ.

**Trading flow: candidate → position, not analysis → position.** Earlier versions had a single "+ 添加到持仓" button after analysis; this turned out to be wrong because AI verdicts often suggest entry prices that aren't yet met. The current model:
- "加为候选（等触发）" — `add_candidate()` with trigger_price = AI's suggested entry, optional `stop_advice` / `target_advice` carried forward from the analysis
- "已成交，记为持仓" — direct `add_position()` for already-filled trades
- `promote_candidate(ts_code, entry_price, stop_loss, target)` — once filled, archives the candidate as `archived_promoted` and creates the active position with **actual** fill values (not the trigger price). Validates no duplicate position **before** archiving the candidate so failures leave state intact for UI recovery.

**Soft delete via `archive_entry`.** Nothing is hard-deleted from the watchlist. `status` field on archived items records the reason: `archived_manual`, `archived_replaced`, `archived_promoted`, `archived_dedup`, `expired`.

**One-shot migration on Streamlit startup.** `migrate_and_backfill()` runs once per session, gated by `st.session_state["_migrated"]`. It normalizes legacy ts_codes, backfills missing names via tushare, merges legacy unsuffixed journal files, and dedups duplicate active positions. Add new one-shot fixes here rather than scattering migration logic.

**Realtime vs daily price.** `get_realtime_price` (Sina, intraday) is the primary; `get_latest_price` (tushare daily close) is the fallback used when realtime returns None (suspended / API failure). The UI caches both in `st.session_state["prices"]` / `["realtime_prices"]` and clears them on refresh or after any mutation.

**Adding an AI tool** requires three coordinated edits: (1) implement the function in `apex/data.py` returning a JSON string, (2) register it in `data.TOOL_FUNCTIONS`, (3) declare its schema in `analyze.TOOLS`. The agent dispatches by name through `_dispatch_tool` which only looks at `TOOL_FUNCTIONS`.
