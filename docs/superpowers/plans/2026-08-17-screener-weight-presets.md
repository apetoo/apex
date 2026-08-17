# Screener Weight Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a recommended automatic strategy-selection mode plus three editable manual weight presets to the screener page.

**Architecture:** Keep preset definitions in a small route-local module and keep HTTP/localStorage serialization in the existing screener API module. `ScreenerPage` owns the selected mode: automatic requests omit `strategy_weights`, while manual modes send the current weights and expose sliders.

**Tech Stack:** React 19, TypeScript 6, TanStack Query, Vitest, Testing Library, localStorage, SSE fetch adapter.

## Global Constraints

- Do not change strategy internals, scoring, factor-health calculations, or report formats.
- Automatic mode must omit `strategy_weights`; backend fallback behavior remains authoritative.
- Manual preset weights must total exactly 1 and remain editable in 0.05 increments.
- Existing `apex.screener.weights` localStorage data must remain readable.
- Do not modify the date-input behavior in this change.

---

### Task 1: Preset and request preference model

**Files:**
- Create: `frontend/src/routes/screener/screenerPresets.ts`
- Create: `frontend/src/routes/screener/__tests__/screenerPresets.test.ts`
- Create: `frontend/src/api/__tests__/screener-preferences.test.ts`
- Modify: `frontend/src/api/screener.ts:19-20,224-268`

**Interfaces:**
- Produces: `ScreenerMode = "auto" | "steady" | "balanced" | "aggressive"`.
- Produces: `SCREENER_PRESETS: Record<Exclude<ScreenerMode, "auto">, ScreenerWeights>`.
- Produces: `screenerFetchFn(weights?: ScreenerWeights)`; `undefined` means automatic mode.
- Produces: `loadScreenerPreferences(defaultWeights): { mode: ScreenerMode; weights: ScreenerWeights }` and `saveScreenerPreferences(preferences): void`.

- [ ] **Step 1: Write failing preset tests**

Assert that every manual preset has all eight strategy keys, totals 1, and matches the approved anchor values: steady `stealth_accumulation=0.30`, balanced `pullback_to_ma=0.20`, aggressive `volume_breakout=0.25`.

- [ ] **Step 2: Run preset tests and verify RED**

Run: `cd frontend && npm test -- src/routes/screener/__tests__/screenerPresets.test.ts`

Expected: FAIL because `screenerPresets.ts` does not exist.

- [ ] **Step 3: Implement preset constants**

Create the mode union, display metadata, and exact weights from the approved design. Export a `clonePreset(mode)` helper so React state never mutates shared constants.

- [ ] **Step 4: Run preset tests and verify GREEN**

Run: `cd frontend && npm test -- src/routes/screener/__tests__/screenerPresets.test.ts`

Expected: all preset tests PASS.

- [ ] **Step 5: Write failing request and persistence tests**

Cover these behaviors with real `Request` payload/localStorage values:

```ts
expect(JSON.parse(await screenerFetchFn(undefined)(params).then(r => r.clone().text())))
  .toEqual({ skip_ai: false, skip_selector: false });
expect(manualBody.strategy_weights).toEqual(weights);
expect(loadScreenerPreferences(defaults)).toEqual({ mode: "auto", weights: defaults });
```

Also seed `apex.screener.weights` with the legacy plain weight object and expect `{ mode: "balanced", weights: mergedWeights }`.

- [ ] **Step 6: Run API tests and verify RED**

Run: `cd frontend && npm test -- src/api/__tests__/screener-preferences.test.ts`

Expected: FAIL because the new preference functions and optional request contract do not exist.

- [ ] **Step 7: Implement the API contract and migration**

Use one new key, `apex.screener.preferences`, for `{ mode, weights }`. Read it first; if absent, read legacy `apex.screener.weights`; otherwise default to automatic mode. Validate the stored mode against the four allowed strings and merge stored weights over backend defaults. Build the request body conditionally:

```ts
const body = {
  ...(weights ? { strategy_weights: weights } : {}),
  skip_ai: false,
  skip_selector: false,
};
```

Retain the old exported weight helpers as compatibility wrappers if other imports still use them.

- [ ] **Step 8: Run API tests and verify GREEN**

Run: `cd frontend && npm test -- src/api/__tests__/screener-preferences.test.ts`

Expected: all preference/request tests PASS.

- [ ] **Step 9: Commit the data-model slice**

```bash
git add frontend/src/routes/screener/screenerPresets.ts frontend/src/routes/screener/__tests__/screenerPresets.test.ts frontend/src/api/screener.ts frontend/src/api/__tests__/screener-preferences.test.ts
git commit -m "feat: add screener weight presets"
```

### Task 2: Screener mode selector and editable manual controls

**Files:**
- Create: `frontend/src/routes/screener/__tests__/ScreenerPage.test.tsx`
- Modify: `frontend/src/routes/screener/ScreenerPage.tsx:52-153,210-250`

**Interfaces:**
- Consumes: `ScreenerMode`, `SCREENER_MODE_OPTIONS`, `clonePreset`, `loadScreenerPreferences`, `saveScreenerPreferences`, and optional `screenerFetchFn` from Task 1.
- Produces: user-visible four-way mode selector and conditional advanced sliders.

- [ ] **Step 1: Write a failing default-mode page test**

Render `ScreenerPage` with query/SSE dependencies stubbed at their module boundaries. Assert `自动适配（推荐）` is selected, the automatic explanation is visible, and no range inputs are rendered.

- [ ] **Step 2: Run the page test and verify RED**

Run: `cd frontend && npm test -- src/routes/screener/__tests__/ScreenerPage.test.tsx`

Expected: FAIL because the page currently always renders eight sliders.

- [ ] **Step 3: Implement mode initialization and selector**

Replace `weightsLoaded` initialization with preference initialization. Render four accessible radio-style buttons. In automatic mode show: `系统将根据市场状态、历史表现和今日候选数自动分配权重；失败时回退等权。`

- [ ] **Step 4: Run the default-mode test and verify GREEN**

Run: `cd frontend && npm test -- src/routes/screener/__tests__/ScreenerPage.test.tsx`

Expected: default-mode test PASS.

- [ ] **Step 5: Write failing manual-preset interaction tests**

Click `稳健波段`, assert eight sliders appear and `stealth_accumulation` displays `0.30`; edit that slider and assert preferences are persisted. Click `自动适配（推荐）` and assert sliders disappear. Trigger `跑粗筛` once in each mode and assert the fetch adapter receives `undefined` for automatic mode and current weights for manual mode.

- [ ] **Step 6: Run interaction tests and verify RED**

Run: `cd frontend && npm test -- src/routes/screener/__tests__/ScreenerPage.test.tsx`

Expected: FAIL because preset selection and conditional request behavior are missing.

- [ ] **Step 7: Implement manual preset behavior and requests**

Selecting a manual mode replaces current weights with `clonePreset(mode)`. Slider changes update only the weights. Persist `{ mode, weights }` after initialization. Call `screenerFetchFn(mode === "auto" ? undefined : weights)` and omit `strategy_weights` from the `connect` body in automatic mode.

- [ ] **Step 8: Run interaction tests and verify GREEN**

Run: `cd frontend && npm test -- src/routes/screener/__tests__/ScreenerPage.test.tsx`

Expected: all page tests PASS with no React warnings.

- [ ] **Step 9: Commit the page slice**

```bash
git add frontend/src/routes/screener/ScreenerPage.tsx frontend/src/routes/screener/__tests__/ScreenerPage.test.tsx
git commit -m "feat: add screener strategy modes"
```

### Task 3: Full frontend verification

**Files:**
- Modify only files required to fix failures directly caused by Tasks 1-2.

**Interfaces:**
- Consumes the finished feature from Tasks 1-2.
- Produces a verified frontend build.

- [ ] **Step 1: Run targeted tests together**

Run: `cd frontend && npm test -- src/routes/screener/__tests__/screenerPresets.test.ts src/api/__tests__/screener-preferences.test.ts src/routes/screener/__tests__/ScreenerPage.test.tsx`

Expected: all targeted tests PASS.

- [ ] **Step 2: Run the full test suite**

Run: `cd frontend && npm test`

Expected: zero failed tests.

- [ ] **Step 3: Run lint and production build**

Run: `cd frontend && npm run lint`

Expected: zero lint errors.

Run: `cd frontend && npm run build`

Expected: TypeScript and Vite exit 0.

- [ ] **Step 4: Inspect the final diff**

Run: `git diff --check HEAD~2..HEAD && git status --short`

Expected: no whitespace errors; only scoped feature files plus pre-existing untracked user files.
