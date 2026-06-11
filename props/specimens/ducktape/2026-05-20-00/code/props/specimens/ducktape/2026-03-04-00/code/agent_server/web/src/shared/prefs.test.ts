import { describe, it, expect, beforeEach } from "vitest";

import { prefs } from "./prefs";

describe("prefs store", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("initializes with defaults when no storage", async () => {
    let val: any;
    const unsub = prefs.subscribe((v) => {
      val = v;
    });
    expect(val).toEqual({
      renderMarkdown: true,
      leftSidebarWidth: 220,
      rightSidebarWidth: 280,
      leftTopHeight: 260,
      showAgentsSidebar: true,
    });
    unsub();
  });

  it("persists changes to localStorage", async () => {
    const unsub = prefs.subscribe(() => {
      // Track subscription but don't need value for this test
    });
    prefs.set({
      renderMarkdown: false,
      leftSidebarWidth: 200,
      rightSidebarWidth: 260,
      leftTopHeight: 300,
      showAgentsSidebar: false,
    });
    unsub();
    const raw = localStorage.getItem("adgn_prefs_v1");
    expect(raw).toBeTruthy();
    const obj = JSON.parse(String(raw));
    expect(obj.renderMarkdown).toBe(false);
    expect(obj.leftSidebarWidth).toBe(200);
  });
});
