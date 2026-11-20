import { describe, expect, it } from "vitest";

import { toolPreview } from "./index";

describe("toolPreview", () => {
  it("renders the routine with explicit or default instructions", () => {
    for (const variant of ["compact", "detailed"] as const) {
      for (const args of [{ text: "Check the queue" }, {}, { text: null }]) {
        expect(toolPreview("haku_routine", "launch_routine", args, variant)).not.toBeNull();
      }
    }
  });

  it("falls back to raw data for malformed routine arguments", () => {
    expect(toolPreview("haku_routine", "launch_routine", { text: 123 }, "detailed")).toBeNull();
    expect(toolPreview("haku_routine", "launch_routine", { extra: true }, "detailed")).toBeNull();
  });

  it("uses raw data for other tools", () => {
    expect(toolPreview("haku_routine", "other_tool", {}, "detailed")).toBeNull();
    expect(toolPreview("tana", "launch_routine", {}, "detailed")).toBeNull();
    expect(toolPreview("tana", "move_node", { nodeId: "t1" }, "compact")).toBeNull();
    expect(toolPreview("gmail", "threads_get", { thread_id: "m1" }, "detailed")).toBeNull();
    expect(toolPreview("google_calendar", "get_event", { event_id: "e1" }, "detailed")).toBeNull();
    expect(toolPreview("grocy-sf", "stock_get", {}, "detailed")).toBeNull();
  });
});
