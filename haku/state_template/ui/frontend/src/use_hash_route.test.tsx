// @vitest-environment jsdom
// The wiring half of hash routing (the pure parse/format half is routes.test.ts): initial
// parse on mount, navigate() writing location.hash, hashchange driving state, junk fallback.
// This is the formal promotion of the pre-deploy Playwright checks' app-logic portion; real
// browser history semantics (back/forward) stay browser behavior, verified by the manual
// Playwright recipe in memory/haku-ui.md when routing is touched.

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { HOME, useHashRoute } from "./routes.ts";

function fireHashChange() {
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

afterEach(() => {
  window.location.hash = "";
});

describe("useHashRoute", () => {
  it("parses the current hash on mount", () => {
    window.location.hash = "#/garden/memory/operator.md";
    const { result } = renderHook(() => useHashRoute());
    expect(result.current[0]).toEqual({ view: "garden", gardenPath: "memory/operator.md" });
  });

  it("navigate() writes the hash and the hashchange event updates state", () => {
    window.location.hash = "";
    const { result } = renderHook(() => useHashRoute());
    expect(result.current[0]).toEqual(HOME);

    act(() => result.current[1]({ view: "runs", gardenPath: null }));
    expect(window.location.hash).toBe("#/runs");
    // jsdom doesn't reliably auto-fire hashchange on assignment — dispatch explicitly,
    // like the browser would.
    act(() => fireHashChange());
    expect(result.current[0]).toEqual({ view: "runs", gardenPath: null });
  });

  it("follows external hash changes (back/forward, manual edit)", () => {
    window.location.hash = "";
    const { result } = renderHook(() => useHashRoute());
    act(() => {
      window.location.hash = "#/improvements";
      fireHashChange();
    });
    expect(result.current[0]).toEqual({ view: "improvements", gardenPath: null });
  });

  it("falls back to Inbox on a junk hash", () => {
    window.location.hash = "";
    const { result } = renderHook(() => useHashRoute());
    act(() => {
      window.location.hash = "#/garbage/route";
      fireHashChange();
    });
    expect(result.current[0]).toEqual(HOME);
  });

  it("navigating to the already-current hash still syncs state", () => {
    window.location.hash = "#/runs";
    const { result } = renderHook(() => useHashRoute());
    act(() => result.current[1]({ view: "runs", gardenPath: null }));
    expect(result.current[0]).toEqual({ view: "runs", gardenPath: null });
  });
});
