import { describe, expect, it } from "vitest";

import { toolCallPreview, toolPreview, toolResultPreview } from "./index.tsx";

// The per-server renderers are covered in each module's own *.test.ts; this covers the
// registry dispatch itself — routing by serverId, threading the variant, and the null paths.
describe("toolPreview registry", () => {
  it("dispatches to the registered renderer for a known serverId, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        toolPreview("gmail", "threads_modify_labels", { thread_ids: ["t1"], add: ["urgent"], remove: [] }, variant)
      ).not.toBeNull();
      expect(toolPreview("tana-rw", "trash_node", { nodeId: "node" }, variant)).not.toBeNull();
    }
  });

  it("returns null for an unregistered server", () => {
    expect(toolPreview("some-other-server", "some_tool", {}, "compact")).toBeNull();
  });

  it("returns null when the server is registered but the tool has no widget", () => {
    expect(toolPreview("gmail", "labels_list", {}, "detailed")).toBeNull();
  });

  it("returns null for a tool that's a combined widget (call registry) instead", () => {
    expect(
      toolPreview(
        "google_calendar",
        "create_event",
        { summary: "Standup", start: { date: "2026-09-15" }, end: { date: "2026-09-16" } },
        "compact"
      )
    ).toBeNull();
  });
});

describe("toolResultPreview registry", () => {
  it("dispatches to the registered renderer for a known serverId, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        toolResultPreview(
          "google_calendar",
          "get_event",
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

  it("returns null for a tool that's a combined widget (call registry) instead", () => {
    expect(toolResultPreview("gmail", "drafts_create", { id: "d1" }, "compact")).toBeNull();
  });
});

describe("toolCallPreview registry", () => {
  it("dispatches to the registered combined widget's pending and finished states, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      const args = { summary: "Standup", start: { date: "2026-09-15" }, end: { date: "2026-09-16" } };
      expect(toolCallPreview("google_calendar", "create_event", args, null, variant)).not.toBeNull();
      expect(
        toolCallPreview(
          "google_calendar",
          "create_event",
          args,
          {
            content: [{ type: "text", text: "{}" }],
            isError: false,
            structuredContent: { event_id: "evt1", html_link: "https://www.google.com/calendar/event?eid=evt1" },
          },
          variant
        )
      ).not.toBeNull();
    }
  });

  it("returns null for an unregistered server", () => {
    expect(toolCallPreview("some-other-server", "some_tool", {}, null, "compact")).toBeNull();
  });

  it("returns null when the server is registered but the tool has no combined widget", () => {
    expect(toolCallPreview("gmail", "threads_modify_labels", { thread_ids: ["t1"] }, null, "detailed")).toBeNull();
  });
});
