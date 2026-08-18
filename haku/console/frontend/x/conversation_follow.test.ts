import { describe, expect, it } from "vitest";

import type { Conversation, ConversationSession, ConversationUpdate } from "../client";
import { followed } from "./conversation_follow";

type Message = ConversationSession["messages"][number];

function message(id: string, content: string, createdAt: string, status: Message["status"] = "complete"): Message {
  return {
    message_id: id,
    role: "assistant",
    status,
    content,
    tool_calls: [],
    error: null,
    source_first_frame_seq: null,
    source_last_frame_seq: null,
    created_at: createdAt,
    updated_at: createdAt,
  };
}

function conversation(messages: Message[]): Conversation {
  return {
    conversation_id: "c1",
    created_at: "2026-08-18T00:00:00Z",
    attachments: [],
    session: {
      session_id: "s1",
      status: "ready",
      error: null,
      created_at: "2026-08-18T00:00:00Z",
      updated_at: "2026-08-18T00:00:00Z",
      provisioning: null,
      narration: [],
      messages,
      turns: [],
    },
    earlier_sessions: [],
  };
}

function update(fields: Partial<ConversationUpdate>): ConversationUpdate {
  return {
    message_type: "update",
    position: 10,
    session_id: "s1",
    status: "ready",
    error: null,
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:01Z",
    provisioning: null,
    narration: [],
    attachments: [],
    earlier_sessions: [],
    messages: [],
    turns: [],
    ...fields,
  };
}

describe("followed", () => {
  it("replaces everything held when a snapshot arrives", () => {
    const fresh = conversation([message("m2", "second", "2026-08-18T00:00:02Z")]);

    expect(
      followed(conversation([message("m1", "first", "2026-08-18T00:00:01Z")]), {
        message_type: "snapshot",
        position: 7,
        conversation: fresh,
      })
    ).toEqual(fresh);
  });

  it("replaces a message it already holds rather than repeating it", () => {
    // The open message arrives once per coalescing window carrying the prose so far, so the same
    // `message_id` lands again and again while a turn is being written.
    const held = conversation([message("m1", "half an ans", "2026-08-18T00:00:01Z", "streaming")]);

    const next = followed(held, update({ messages: [message("m1", "half an answer", "2026-08-18T00:00:01Z")] }));

    expect(next.session.messages.map((row) => [row.message_id, row.content, row.status])).toEqual([
      ["m1", "half an answer", "complete"],
    ]);
  });

  it("puts an arriving message in transcript order, not arrival order", () => {
    const held = conversation([message("m2", "answer", "2026-08-18T00:00:02Z")]);

    const next = followed(held, update({ messages: [message("m1", "prompt", "2026-08-18T00:00:01Z")] }));

    expect(next.session.messages.map((row) => row.message_id)).toEqual(["m1", "m2"]);
  });

  it("applying one update twice is applying it once", () => {
    // Delivery is not exactly-once by design: re-reading from an older position is always correct,
    // which is only true if the merge is.
    const held = conversation([message("m1", "first", "2026-08-18T00:00:01Z")]);
    const arriving = update({ messages: [message("m2", "second", "2026-08-18T00:00:02Z")] });

    expect(followed(followed(held, arriving), arriving)).toEqual(followed(held, arriving));
  });

  it("takes the whole live session row, so nothing describes a replaced session", () => {
    const held = conversation([message("m1", "before the sandbox died", "2026-08-18T00:00:01Z")]);

    const next = followed(
      held,
      update({
        session_id: "s2",
        created_at: "2026-08-18T01:00:00Z",
        updated_at: "2026-08-18T01:00:05Z",
        earlier_sessions: [{ session_id: "s1", status: "failed", created_at: "2026-08-18T00:00:00Z" }],
      })
    );

    expect(next.session.session_id).toBe("s2");
    expect(next.session.created_at).toBe("2026-08-18T01:00:00Z");
    expect(next.earlier_sessions.map((row) => row.session_id)).toEqual(["s1"]);
  });

  it("takes the channels holding the conversation as they are now", () => {
    const held = conversation([]);

    const next = followed(
      held,
      update({
        attachments: [{ surface: "matrix", address: "!room:example.org", attached_at: "2026-08-18T00:30:00Z" }],
      })
    );

    expect(next.attachments.map((row) => row.address)).toEqual(["!room:example.org"]);
  });

  it("refuses an update with nothing to merge into", () => {
    // A position is only ever sent back after a snapshot established what it addresses, so this is
    // a protocol violation rather than an empty view to render.
    expect(() => followed(null, update({}))).toThrow();
  });
});
