import { describe, expect, it } from "vitest";

import type { ClaudeChatMessage } from "../client";
import { conversationTimeline, type ConversationTurn } from "./conversation_timeline";

function message(id: string, createdAt: string): ClaudeChatMessage {
  return {
    message_id: id,
    role: "user",
    status: "complete",
    content: id,
    tool_uses: [],
    error: null,
    created_at: createdAt,
    updated_at: createdAt,
  };
}

function turn(id: string, startedAt: string): ConversationTurn {
  return {
    turn_id: id,
    started_at: startedAt,
    ended_at: null,
    outcome: "answered",
    cost_usd: null,
    duration_ms: null,
    usage: null,
  };
}

function shape(entries: ReturnType<typeof conversationTimeline>): string[] {
  return entries.map((entry) =>
    entry.kind === "message" ? entry.message.message_id : `turn ${entry.number} (${entry.turn.turn_id})`
  );
}

describe("conversationTimeline", () => {
  it("numbers turns in transcript order even though the endpoint returns them newest-first", () => {
    const entries = conversationTimeline(
      [message("m1", "2026-08-01T03:00:00Z"), message("m2", "2026-08-01T03:05:00Z")],
      [turn("t2", "2026-08-01T03:04:00Z"), turn("t1", "2026-08-01T03:00:01Z")]
    );
    expect(shape(entries)).toEqual(["m1", "turn 1 (t1)", "turn 2 (t2)", "m2"]);
  });

  it("places a turn before the first message it could have produced", () => {
    const entries = conversationTimeline(
      [
        message("prompt", "2026-08-01T03:00:00Z"),
        message("answer", "2026-08-01T03:00:09Z"),
        message("next-prompt", "2026-08-01T03:01:00Z"),
      ],
      [turn("t1", "2026-08-01T03:00:01Z")]
    );
    expect(shape(entries)).toEqual(["prompt", "turn 1 (t1)", "answer", "next-prompt"]);
  });

  it("trails a turn that started after the last recorded message", () => {
    const entries = conversationTimeline(
      [message("m1", "2026-08-01T03:00:00Z")],
      [turn("running", "2026-08-01T03:09:00Z")]
    );
    expect(shape(entries)).toEqual(["m1", "turn 1 (running)"]);
  });

  it("orders by instant, not by the string a sub-second timestamp prints as", () => {
    const entries = conversationTimeline(
      [],
      [turn("later", "2026-08-01T03:00:11Z"), turn("earlier", "2026-08-01T03:00:10.5Z")]
    );
    expect(shape(entries)).toEqual(["turn 1 (earlier)", "turn 2 (later)"]);
  });

  it("renders a transcript with no recorded turns unchanged", () => {
    expect(shape(conversationTimeline([message("m1", "2026-08-01T03:00:00Z")], []))).toEqual(["m1"]);
  });
});
