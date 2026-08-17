import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  loadScreenerPreferences,
  saveScreenerPreferences,
  screenerFetchFn,
  type ScreenerWeights,
} from "../screener";

const defaults: ScreenerWeights = {
  first_board_leader: 0.5,
  stealth_accumulation: 0.5,
};

const requestParams = {
  path: "/api/screener/run",
  method: "POST",
  body: {},
  signal: new AbortController().signal,
};

describe("screener request mode", () => {
  const fetchSpy = vi.fn(
    async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(null, { status: 200 }),
  );

  beforeEach(() => {
    fetchSpy.mockClear();
    vi.spyOn(globalThis, "fetch").mockImplementation(fetchSpy);
  });

  afterEach(() => vi.restoreAllMocks());

  it("omits manual weights when automatic selection is requested", async () => {
    await screenerFetchFn(undefined)(requestParams);

    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      skip_ai: false,
      skip_selector: false,
    });
  });

  it("sends current weights for a manual preset", async () => {
    await screenerFetchFn(defaults)(requestParams);

    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      strategy_weights: defaults,
      skip_ai: false,
      skip_selector: false,
    });
  });
});

describe("screener preference persistence", () => {
  beforeEach(() => window.localStorage.clear());

  it("defaults new users to automatic selection", () => {
    expect(loadScreenerPreferences(defaults)).toEqual({
      mode: "auto",
      weights: defaults,
    });
  });

  it("restores the selected mode and merges saved weights over defaults", () => {
    window.localStorage.setItem(
      "apex.screener.preferences",
      JSON.stringify({ mode: "steady", weights: { stealth_accumulation: 0.3 } }),
    );

    expect(loadScreenerPreferences(defaults)).toEqual({
      mode: "steady",
      weights: {
        first_board_leader: 0.5,
        stealth_accumulation: 0.3,
      },
    });
  });

  it("migrates legacy plain weights to a manual balanced session", () => {
    window.localStorage.setItem(
      "apex.screener.weights",
      JSON.stringify({ stealth_accumulation: 0.3 }),
    );

    expect(loadScreenerPreferences(defaults)).toEqual({
      mode: "balanced",
      weights: {
        first_board_leader: 0.5,
        stealth_accumulation: 0.3,
      },
    });
  });

  it("saves mode and weights together", () => {
    saveScreenerPreferences({ mode: "aggressive", weights: defaults });

    expect(JSON.parse(window.localStorage.getItem("apex.screener.preferences") ?? "null")).toEqual({
      mode: "aggressive",
      weights: defaults,
    });
  });
});
