import { describe, expect, it } from "vitest";

import { toolPreview } from "./index.tsx";

// The per-server renderers are covered in each module's own *.test.ts; this covers the
// registry dispatch itself — routing by serverId, threading the variant, and the null paths.
describe("toolPreview registry", () => {
  it("dispatches to the registered renderer for a known serverId, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        toolPreview("gmail", "threads_batch_modify", { thread_ids: ["t1"], add: ["urgent"], remove: [] }, variant)
      ).not.toBeNull();
      expect(
        toolPreview(
          "google_calendar",
          "create_calendar_event",
          { summary: "Standup", start: { date: "2026-09-15" }, end: { date: "2026-09-16" } },
          variant
        )
      ).not.toBeNull();
    }
  });

  it("returns null for an unregistered server", () => {
    expect(toolPreview("some-other-server", "some_tool", {}, "compact")).toBeNull();
  });

  it("returns null when the server is registered but the tool has no widget", () => {
    expect(toolPreview("gmail", "threads_list", { query: "from:a" }, "detailed")).toBeNull();
  });
});
