import { describe, expect, it } from "vitest";

import { toolPreview } from "./index.tsx";

// The per-server renderers are covered in each module's own *.test.ts; this covers the
// registry dispatch itself — routing by serverId and the two null paths.
describe("toolPreview registry", () => {
  it("dispatches to the registered renderer for a known serverId", () => {
    expect(
      toolPreview("grocy-sf", "products_create", {
        items: [{ name: "Oats", stock_qu: "Gram", location: "Pantry", default_best_before_days: 270 }],
      })
    ).not.toBeNull();
    expect(
      toolPreview("google", "create_calendar_event", {
        summary: "Standup",
        start: { date: "2026-09-15" },
        end: { date: "2026-09-16" },
      })
    ).not.toBeNull();
  });

  it("returns null for an unregistered server", () => {
    expect(toolPreview("some-other-server", "some_tool", {})).toBeNull();
  });

  it("returns null when the server is registered but the tool has no widget", () => {
    expect(toolPreview("google", "list_events", {})).toBeNull();
  });
});
