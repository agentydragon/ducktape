import { describe, expect, it } from "vitest";

import { toolResultPreview } from "./index.tsx";

// The per-server renderers are covered in each module's own *.test.ts; this covers the
// registry dispatch itself — routing by serverId, threading the variant, and the null paths.
describe("toolResultPreview registry", () => {
  it("dispatches to the registered renderer for a known serverId, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(toolResultPreview("gmail", "drafts_create", { id: "d1" }, variant)).not.toBeNull();
      expect(
        toolResultPreview(
          "google_calendar",
          "create_calendar_event",
          { event_id: "evt1", html_link: "https://www.google.com/calendar/event?eid=evt1" },
          variant
        )
      ).not.toBeNull();
      expect(
        toolResultPreview(
          "grocy-sf",
          "stock_add",
          [{ kind: "ok", product_name: "Oats", qu_name: "Gram", location_name: "Pantry" }],
          variant
        )
      ).not.toBeNull();
    }
  });

  it("returns null for an unregistered server", () => {
    expect(toolResultPreview("some-other-server", "some_tool", {}, "compact")).toBeNull();
  });

  it("returns null when the server is registered but the tool has no result widget", () => {
    expect(toolResultPreview("gmail", "threads_modify_labels", { modified: 3 }, "detailed")).toBeNull();
  });
});
