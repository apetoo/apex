# Chan Decision Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing single-timeframe Chan structure into an explainable decision card with deterministic confirmation/invalidation levels and a reviewed path into the existing candidate workflow.

**Architecture:** Keep czsc segmentation unchanged. Add a pure backend decision builder over serialized completed bars, BSPs, and the ex-dividend-filtered confirmed pivot; expose one additive `decision` object, then render it and submit reviewed candidate parameters through the existing watchlist mutation.

**Tech Stack:** Python 3, FastAPI, czsc 0.10.12, pytest, React 19, TypeScript, TanStack Query, Vitest, Testing Library.

## Global Constraints

- Use only completed K bars; do not introduce future data into state classification.
- Preserve every existing `/api/chan/{ts_code}` field and all current error/degradation behavior.
- Keep buy/sell points labeled as single-timeframe approximations without sub-timeframe confirmation.
- Candidate writes require an editable confirmation dialog; never auto-submit.
- Do not implement multi-timeframe aggregation, scanning, divergence, target projection, backtesting, AI prompt changes, or automatic trading.
- Preserve unrelated dirty-worktree changes and stage only files named by each task.

---

### Task 1: Pure Chan decision model

**Files:**
- Modify: `apex/chan.py`
- Test: `tests/test_chan.py`

**Interfaces:**
- Produces: `_build_decision(bars: list[dict], bsp_list: list[dict], zs_break: str, last_confirmed_zs: Optional[dict], freq: str) -> dict`
- Produces: top-level `decision` in every `get_structure` response, including insufficient-bars responses.

- [ ] **Step 1: Write failing unit tests for the stable empty decision and buy-point lifecycle**

Add a `TestChanDecision` class using small serialized fixtures. Assert the exact stable empty shape:

```python
EMPTY_DECISION = {
    "bias": "neutral",
    "setup": "none",
    "state": "watching",
    "bsp_type": None,
    "signal_dt": None,
    "bars_since_signal": None,
    "confirm_price": None,
    "invalidation_price": None,
    "trigger_price": None,
    "trigger_low": None,
    "trigger_high": None,
    "candidate_eligible": False,
    "ineligible_reason": "no_actionable_structure",
    "basis": ["暂无可执行的做多结构"],
}
```

Cover these buy cases with `type="2buy"`, `price=10.0`, signal-bar high `10.5`:

```python
decision = chan._build_decision(bars, [bsp], "none", None, "D")
assert decision["confirm_price"] == 10.5
assert decision["invalidation_price"] == 10.0
assert decision["state"] == "pending"
assert decision["candidate_eligible"] is True
```

Then add separate assertions for a later close above `10.5` (`confirmed`), a current close below `10.0` (`invalid`, ineligible), and `bars_since_signal == 11` (`ineligible_reason="stale_signal"`). Assert an invalid current close wins even if an earlier close confirmed the signal.

- [ ] **Step 2: Run the decision tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_chan.py::TestChanDecision -q`

Expected: FAIL because `_build_decision` does not exist.

- [ ] **Step 3: Implement normalized default output and buy-point evaluation**

In `apex/chan.py`, add constants for buy/sell BSP types and a constructor that always returns every key. Match a BSP to its completed bar by `_fmt_dt(bar["dt"], freq) == bsp["dt"]`; if no match exists, return `watching` with `ineligible_reason="signal_bar_missing"`.

For the most recent buy BSP:

```python
signal_idx = matching_bar_index
bars_since = len(bars) - 1 - signal_idx
confirm = float(bars[signal_idx]["high"])
invalid = float(bsp["price"])
confirmed = any(float(b["close"]) > confirm for b in bars[signal_idx + 1:])
is_invalid = float(bars[-1]["close"]) < invalid
state = "invalid" if is_invalid else "confirmed" if confirmed else "pending"
eligible = state != "invalid" and bars_since <= 10
```

Set `setup="bsp_buy"`, `bias="long"` unless invalid, `trigger_price=confirm`, no trigger band, and deterministic Chinese basis strings containing the BSP label, confirmation rule, and invalidation rule. Preserve the BSP subtype in a new `bsp_type` field so the UI can write a specific setup; add `bsp_type=None` to the empty shape.

- [ ] **Step 4: Add failing tests for breakout, risk, and precedence**

Cover:

- `zs_break="up"` with `{zg: 12, zd: 10}` returns `setup="zs_breakout"`, `state="confirmed"`, `trigger_price=12`, `trigger_low=12`, `trigger_high=12.12`, `invalidation_price=10`, eligible.
- `zs_break="down"` returns `bias="risk"`, `state="invalid"`, ineligible.
- a recent sell BSP with no valid recent buy returns a risk decision.
- a recent valid buy outranks an upward breakout.
- an expired buy falls through to an upward breakout; if there is no breakout, it remains visible but ineligible as stale.
- missing pivot data never creates candidate prices.

- [ ] **Step 5: Implement breakout/risk precedence and run focused tests**

Use priority: valid buy within 10 bars; upward breakout; latest sell/downward break risk; expired buy; watching. Round calculated prices to three decimals, consistent with current Chan serialization.

Run: `.venv/bin/python -m pytest tests/test_chan.py::TestChanDecision -q`

Expected: PASS.

- [ ] **Step 6: Integrate the decision into `get_structure` and update HTTP shape tests**

Initialize `resp["decision"]` with the empty decision before the insufficient-bars return. After `_zs_break`, call `_build_decision` with the raw completed bars, serialized BSP list, break state, and filtered confirmed pivot. Update `TestHttp.test_200_shape` to include `decision`; add an insufficient-bars assertion that its decision is `watching` and ineligible.

- [ ] **Step 7: Run all Chan backend tests and commit**

Run: `.venv/bin/python -m pytest tests/test_chan.py -q`

Expected: all tests pass.

Commit only `apex/chan.py` and `tests/test_chan.py` with message `feat(chan): add actionable decision model`.

---

### Task 2: Frontend contract and candidate payload support

**Files:**
- Modify: `frontend/src/api/chan.ts`
- Modify: `frontend/src/api/watchlist.ts`
- Test: `frontend/src/routes/chan/__tests__/ChanPage.test.tsx`

**Interfaces:**
- Produces: `ChanDecisionBias`, `ChanDecisionSetup`, `ChanDecisionState`, and `ChanDecision` matching the backend object.
- Changes: `ChanStructure.decision: ChanDecision` becomes required.
- Changes: `AddCandidatePayload` accepts `strategy?: string` so the existing backend field is type-safe.

- [ ] **Step 1: Extend the TypeScript API contract**

Add literal unions matching Task 1 and this interface:

```ts
export interface ChanDecision {
  bias: "long" | "neutral" | "risk";
  setup: "bsp_buy" | "zs_breakout" | "none";
  state: "watching" | "pending" | "confirmed" | "invalid";
  bsp_type: ChanBsp["type"] | null;
  signal_dt: string | null;
  bars_since_signal: number | null;
  confirm_price: number | null;
  invalidation_price: number | null;
  trigger_price: number | null;
  trigger_low: number | null;
  trigger_high: number | null;
  candidate_eligible: boolean;
  ineligible_reason: string | null;
  basis: string[];
}
```

Add `decision: ChanDecision` to `ChanStructure`, and `strategy?: string` to `AddCandidatePayload`. Do not loosen fields to `string` or `unknown`.

- [ ] **Step 2: Update the Chan test fixture and verify type-checking**

Add a `fullDecision()` factory to `ChanPage.test.tsx` and include it in `fullStructure`. Default it to an eligible pending `2buy` decision so later UI tests can override one field at a time.

Run: `cd frontend && npm run build`

Expected: TypeScript passes; Vite build completes.

- [ ] **Step 3: Commit the contract change**

Commit only the three files named in this task with message `feat(frontend): type chan decision contract`.

---

### Task 3: Decision card and reviewed candidate dialog

**Files:**
- Modify: `frontend/src/routes/chan/ChanPage.tsx`
- Test: `frontend/src/routes/chan/__tests__/ChanPage.test.tsx`

**Interfaces:**
- Consumes: `ChanStructure.decision`, `getWatchlist`, `useAddCandidate`, and the existing base `Dialog`.
- Produces: no new route or persisted browser state.

- [ ] **Step 1: Write failing rendering and eligibility tests**

Mock `getWatchlist` and `useAddCandidate` at module level. Add parameterized tests asserting:

- `pending`: shows “等待确认”, confirm and invalidation prices, basis, enabled candidate button.
- `confirmed`: shows “已确认” and the anti-chasing reminder.
- `invalid`: shows “已失效” and disabled candidate button.
- `watching` or `risk`: shows the correct label and disabled candidate button.
- `bars_since_signal` renders as “距今 N 根”.

Keep the existing structure metrics below the new decision headline so no current information disappears.

- [ ] **Step 2: Implement the decision card presentation**

Rename `SummaryPanel` to `DecisionPanel`. Define exhaustive label/style maps for state and bias. Render nullable prices as `—`; render every `basis` item as a short list. Disable candidate action when `candidate_eligible` is false and show `ineligible_reason` as explanatory copy rather than relying only on disabled state.

Run: `cd frontend && npx vitest run src/routes/chan/__tests__/ChanPage.test.tsx`

Expected: rendering tests pass; dialog tests added next still fail.

- [ ] **Step 3: Write failing candidate-dialog tests**

Test two setups:

1. Eligible buy point opens the dialog with code, frequency, trigger `10.50`, stop `10.00`, `strategy="chan"` in the submitted payload, setup label such as `缠论二买`, no `target_advice`, and an editable note.
2. Breakout opens with `trigger_low=12.00`, `trigger_high=12.12`, `trigger_price=12.00`, setup `中枢突破回踩`.

Also test manual edits, mutation failure preserving the dialog/input, successful mutation closing the dialog and showing “已加入候选”, and a matching code in mocked watchlist data showing “将覆盖原候选参数” before submit.

- [ ] **Step 4: Implement the dialog and submission flow**

Use controlled string state for editable numeric inputs, initialized whenever the dialog opens. Query `getWatchlist` with existing `qk.watchlist` to detect an existing candidate; a watchlist read failure must not block submission, but it cannot claim there is no existing candidate.

Build the payload only on submit:

```ts
{
  ts_code: data.ts_code,
  name: "",
  trigger_price: Number(triggerPrice),
  trigger_direction: decision.setup === "zs_breakout" ? "below" : "above",
  trigger_low: triggerLow ? Number(triggerLow) : undefined,
  trigger_high: triggerHigh ? Number(triggerHigh) : undefined,
  stop_advice: stopPrice ? Number(stopPrice) : undefined,
  note,
  strategy: "chan",
  setup: decision.setup === "zs_breakout"
    ? "中枢突破回踩"
    : `缠论${CHAN_BSP_LABEL[decision.bsp_type ?? ""] ?? "买点"}`,
}
```

For a retest band, `trigger_direction` is compatibility metadata only because existing `is_triggered` prioritizes `trigger_low/trigger_high`. Validate positive prices and `trigger_low <= trigger_high`; show inline errors and do not mutate on invalid input. Pass `busy={mutation.isPending}` to `Dialog`. Use mutation callbacks to retain the form on error and clear/close it on success. Use a local success message rather than introducing a notification dependency.

- [ ] **Step 5: Preserve AI navigation and disclosure behavior**

Keep the existing AI-analysis sessionStorage seeding and link. Keep completed-bar, approximation, repaint, ex-dividend, and 250-bar-window disclosures unchanged.

- [ ] **Step 6: Run focused frontend tests and commit**

Run: `cd frontend && npx vitest run src/routes/chan/__tests__/ChanPage.test.tsx`

Expected: all Chan page tests pass.

Commit only `frontend/src/routes/chan/ChanPage.tsx` and its test with message `feat(chan): add decision card candidate flow`.

---

### Task 4: Documentation and full verification

**Files:**
- Modify: `CLAUDE.md`
- Modify: `CONTRIBUTING.md` only if its API/UI conventions need an additive clarification discovered during implementation.

**Interfaces:**
- Documents: additive `/api/chan` decision contract and Chan-to-candidate workflow.

- [ ] **Step 1: Update project guidance**

Document that `/api/chan/{ts_code}` now includes the additive `decision` object, that decisions use completed bars only, and that candidates are user-confirmed with `strategy="chan"`. Keep the single-timeframe approximation and repaint warnings explicit.

- [ ] **Step 2: Run complete backend verification**

Run: `.venv/bin/python -m pytest -q`

Expected: all backend tests pass; the known Starlette `TestClient` deprecation warning may remain.

- [ ] **Step 3: Run complete frontend verification**

Run: `cd frontend && npx vitest run`

Expected: all frontend tests pass.

Run: `cd frontend && npm run build`

Expected: TypeScript and Vite build pass; the existing large-chunk warning is non-blocking.

- [ ] **Step 4: Check patch hygiene**

Run: `git diff --check`

Expected: no whitespace errors. Inspect `git status --short` and ensure no unrelated dirty files are staged.

- [ ] **Step 5: Commit documentation**

Commit only documentation changed by this task with message `docs: document chan decision workflow`.

---

## Acceptance Checklist

- A user can distinguish watching, pending, confirmed, invalid, and risk outcomes without interpreting raw overlays.
- Every actionable buy decision exposes fixed, explainable confirmation and invalidation levels derived only from completed bars.
- Signals older than 10 bars or invalid structures cannot be submitted as candidates.
- Upward pivot breaks create a 1% retest band instead of an immediate chase entry.
- Candidate parameters are editable and require explicit confirmation; targets remain unset.
- Existing Chan response fields, chart overlays, disclosures, AI navigation, watchlist upsert behavior, and error paths remain compatible.
