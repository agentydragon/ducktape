import { describe, expect, it } from "vitest";

import type { SessionEvent } from "./api";
import { foldSessionEvents } from "./transcript";

function event(sequence: number, event_type: string, payload: Record<string, unknown>): SessionEvent {
  return {
    event_id: `event-${sequence}`,
    sequence_num: String(sequence),
    event_type,
    source: event_type === "user" ? "client" : "worker",
    created_at: `2026-09-30T18:4${sequence}:00Z`,
    received_at: null,
    processing_at: null,
    processed_at: null,
    device_attestation_status: "DEVICE_ATTESTATION_STATUS_UNSPECIFIED",
    sent_by_account_id: null,
    payload,
  };
}

describe("foldSessionEvents", () => {
  it("turns messages, tool results, task updates, and turn usage into ordered transcript items", () => {
    const folded = foldSessionEvents([
      event(6, "assistant", {
        type: "assistant",
        message: { content: [{ type: "text", text: "Here is the result." }] },
      }),
      event(3, "user", {
        type: "user",
        message: {
          content: [
            {
              type: "tool_result",
              tool_use_id: "tool-read",
              content: [{ type: "text", text: "Found the failing assertion." }],
            },
          ],
        },
      }),
      event(1, "user", { type: "user", message: { content: [{ type: "text", text: "Find the broken test." }] } }),
      event(4, "system", {
        type: "system",
        subtype: "task_started",
        task_id: "task-tests",
        description: "Inspect the test suite",
      }),
      event(5, "system", {
        type: "system",
        subtype: "task_notification",
        task_id: "task-tests",
        status: "completed",
        summary: "One assertion fails.",
      }),
      event(2, "assistant", {
        type: "assistant",
        message: {
          content: [
            { type: "thinking", thinking: "I should inspect the failing test and its fixture." },
            { type: "text", text: "I will inspect the test." },
            { type: "tool_use", id: "tool-read", name: "Read", input: { file_path: "tests/test_viewer.py" } },
          ],
        },
      }),
      event(7, "result", { type: "result", usage: { total_tokens: 2315 }, total_cost_usd: 0.0123, duration_ms: 2000 }),
    ]);

    expect(folded.map((item) => item.kind)).toEqual([
      "message",
      "thinking",
      "message",
      "tool",
      "activity",
      "message",
      "summary",
    ]);
    expect(folded[0]).toMatchObject({ kind: "message", role: "user", text: "Find the broken test." });
    expect(folded[1]).toMatchObject({ kind: "thinking", text: "I should inspect the failing test and its fixture." });
    expect(folded[2]).toMatchObject({ kind: "message", role: "assistant", text: "I will inspect the test." });
    expect(folded[3]).toMatchObject({
      kind: "tool",
      name: "Read",
      input: { file_path: "tests/test_viewer.py" },
      result: "Found the failing assertion.",
      status: "complete",
    });
    expect(folded[3]?.events).toHaveLength(2);
    expect(folded[4]).toMatchObject({
      kind: "activity",
      title: "Inspect the test suite",
      detail: "One assertion fails.",
      status: "completed",
    });
    expect(folded[4]?.events).toHaveLength(2);
    expect(folded[6]).toMatchObject({
      kind: "summary",
      title: "Turn complete",
      details: ["2,315 tokens", "$0.0123", "2.0 seconds"],
    });
  });

  it("keeps unknown event types readable and preserves their source event", () => {
    const [notice] = foldSessionEvents([event(1, "rate_limit_event", { message: "Try again in a moment." })]);
    expect(notice).toMatchObject({ kind: "notice", title: "rate limit event", detail: "Try again in a moment." });
    expect(notice?.events[0]?.event_id).toBe("event-1");
  });
});
