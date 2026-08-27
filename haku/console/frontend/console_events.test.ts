import { describe, expect, it } from "vitest";

import { changedConversationId } from "./console_events";

// Declared rather than passed inline: `ConsoleEvent` names only the field every event has, and an
// object literal handed straight to a parameter of that type is checked for excess properties —
// which every real event on this channel has.
const conversationChanged = { event_type: "conversation_changed", conversation_id: "a-conversation" };
const toolCallsChanged = { event_type: "tool_calls_changed", tool_call_id: "tc_1" };

describe("changedConversationId", () => {
  it("names the conversation a conversation_changed event invalidates", () => {
    expect(changedConversationId(conversationChanged)).toBe("a-conversation");
  });

  it("returns null for every other event, so a page can skip conversation traffic it does not want", () => {
    expect(changedConversationId(toolCallsChanged)).toBeNull();
    expect(changedConversationId({ event_type: "sync" })).toBeNull();
  });

  it("returns null for a conversation_changed carrying no id, rather than an unusable value", () => {
    // The socket is a wire contract across a `maxUnavailable: 0` roll, so a bundle can meet an
    // event a replica on the other image sent.
    expect(changedConversationId({ event_type: "conversation_changed" })).toBeNull();
  });
});
