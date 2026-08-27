import { describe, expect, it } from "vitest";

import type { Conversation, ConversationEntry, ConversationUpdate } from "../client";
import { followed } from "./conversation_follow";

function message(opened: number, text: string, status: ConversationEntry["status"] = "complete"): ConversationEntry {
  return {
    kind: "message",
    opened_seq: opened,
    closed_seq: status === "open" ? null : opened + 1,
    status,
    provenance: { kind: "authored" },
    text,
    backend_item_id: null,
  };
}

function conversation(entries: ConversationEntry[]): Conversation {
  return {
    conversation_id: "c1",
    runtime_kind: "claude_code",
    created_at: "2026-08-18T00:00:00Z",
    attachments: [],
    entries,
    session: {
      session_id: "s1",
      status: "ready",
      error: null,
      created_at: "2026-08-18T00:00:00Z",
      updated_at: "2026-08-18T00:00:00Z",
      provisioning: null,
      narration: [],
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
    entries: [],
    ...fields,
  };
}

describe("followed", () => {
  it("replaces everything held when a snapshot arrives", () => {
    const fresh = conversation([message(2, "second")]);

    expect(
      followed(conversation([message(1, "first")]), {
        message_type: "snapshot",
        position: 7,
        conversation: fresh,
      })
    ).toEqual(fresh);
  });

  it("merges arriving rows over the ones held, keyed by opening position", () => {
    const held = conversation([message(1, "first")]);

    const next = followed(held, update({ entries: [message(1, "first"), message(5, "second")] }));

    expect(next.entries.map((entry) => entry.opened_seq)).toEqual([1, 5]);
  });

  it("replaces a row it already holds rather than repeating it", () => {
    // A message being written arrives once per coalescing window carrying the prose so far, so
    // the same row lands again and again — newer state, same position.
    const held = conversation([message(1, "half an ans", "open")]);

    const next = followed(held, update({ entries: [message(1, "half an answer")] }));

    expect(next.entries.map((entry) => [entry.opened_seq, "text" in entry ? entry.text : "", entry.status])).toEqual([
      [1, "half an answer", "complete"],
    ]);
  });

  it("puts an arriving row in opening order, not arrival order", () => {
    const held = conversation([message(5, "answer")]);

    const next = followed(held, update({ entries: [message(2, "prompt")] }));

    expect(next.entries.map((entry) => entry.opened_seq)).toEqual([2, 5]);
  });

  it("applying one update twice is applying it once", () => {
    // Delivery is not exactly-once by design: re-reading from an older position is always correct,
    // which is only true if the merge is.
    const held = conversation([message(1, "first")]);
    const arriving = update({ entries: [message(5, "second")] });

    expect(followed(followed(held, arriving), arriving)).toEqual(followed(held, arriving));
  });

  it("takes the whole live session row, so nothing describes a replaced session", () => {
    const held = conversation([message(1, "before the sandbox died")]);

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
