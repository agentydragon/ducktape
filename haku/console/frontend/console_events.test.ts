import { describe, expect, it } from "vitest";

import { changedSessionId } from "./console_events";

// Declared rather than passed inline: `ConsoleEvent` names only the field every event has, and an
// object literal handed straight to a parameter of that type is checked for excess properties —
// which every real event on this channel has.
const sessionChanged = { event_type: "session_changed", session_id: "a-session" };
const toolCallsChanged = { event_type: "tool_calls_changed", tool_call_id: "tc_1" };

describe("changedSessionId", () => {
  it("names the session a session_changed event invalidates", () => {
    expect(changedSessionId(sessionChanged)).toBe("a-session");
  });

  it("returns null for every other event, so a page can skip session traffic it does not want", () => {
    expect(changedSessionId(toolCallsChanged)).toBeNull();
    expect(changedSessionId({ event_type: "sync" })).toBeNull();
  });

  it("returns null for a session_changed carrying no id, rather than an unusable value", () => {
    // The socket is a wire contract across a `maxUnavailable: 0` roll, so a bundle can meet an
    // event a replica on the other image sent.
    expect(changedSessionId({ event_type: "session_changed" })).toBeNull();
  });
});
