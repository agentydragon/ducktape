import { describe, expect, it } from "vitest";

import { toolPreview, toolResultPreview } from "./index";

// The per-server renderers are covered in each module's own *.test.ts; this covers the
// registry dispatch itself — routing by serverId, threading the variant, and the null paths.
describe("toolPreview registry", () => {
  it("dispatches to the registered renderer for a known serverId, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(toolPreview("haku_routine", "launch_routine", { text: "Check the queue" }, variant)).not.toBeNull();
    }
  });

  it("uses recorded data for Gmail and Tana calls handled by Agentplane", () => {
    for (const variant of ["compact", "detailed"] as const) {
      for (const name of ["threads_get", "messages_get", "threads_modify_labels"]) {
        expect(toolPreview("gmail", name, { id: "thread" }, variant)).toBeNull();
        expect(toolResultPreview("gmail", name, { id: "thread" }, variant)).toBeNull();
      }
      for (const name of ["edit_node", "import_tana_paste", "move_node", "set_field_option", "trash_node"]) {
        expect(toolPreview("tana", name, { nodeId: "node" }, variant)).toBeNull();
      }
    }
  });

  it("uses recorded data for Google Calendar", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(toolPreview("google_calendar", "create_event", { summary: "Standup" }, variant)).toBeNull();
      expect(toolResultPreview("google_calendar", "get_event", { event_id: "evt1" }, variant)).toBeNull();
    }
  });
});

describe("toolResultPreview registry", () => {
  it("dispatches to the registered renderer for a known serverId, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
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
});
