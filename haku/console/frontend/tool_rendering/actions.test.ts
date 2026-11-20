import { describe, expect, it } from "vitest";

import { isDestructiveToolCall } from "./actions";

describe("isDestructiveToolCall", () => {
  it("keeps the danger cue on historical destructive tools", () => {
    expect(isDestructiveToolCall("tana", "trash_node")).toBe(true);
    expect(isDestructiveToolCall("google_calendar", "delete_event")).toBe(true);
    expect(isDestructiveToolCall("grocy-sf", "shopping_list_items_remove")).toBe(true);
  });

  it("matches the stored server and tool exactly", () => {
    expect(isDestructiveToolCall("tana", "delete_event")).toBe(false);
    expect(isDestructiveToolCall("haku_routine", "launch_routine")).toBe(false);
  });
});
