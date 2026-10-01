import { describe, expect, it } from "vitest";

import type { SessionEvent } from "./api";
import { adaptSessionEvent, foldSessionEvents } from "./transcript";

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

describe("adaptSessionEvent", () => {
  it("maps Ducktape event envelopes to the message-shaped fold input and keeps the source event", () => {
    const source = event(1, "assistant_message", {
      type: "assistant_message",
      message: { content: [{ type: "text", text: "Hello" }] },
    });
    expect(adaptSessionEvent(source)).toEqual({
      sourceEvent: source,
      type: "assistant",
      payload: source.payload,
    });
  });
});

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
      "tool-run",
      "activity",
      "message",
      "summary",
    ]);
    expect(folded[0]).toMatchObject({ kind: "message", role: "user", text: "Find the broken test." });
    expect(folded[1]).toMatchObject({ kind: "thinking", text: "I should inspect the failing test and its fixture." });
    expect(folded[2]).toMatchObject({ kind: "message", role: "assistant", text: "I will inspect the test." });
    expect(folded[3]).toMatchObject({
      kind: "tool-run",
      tools: [
        {
          name: "Read",
          input: { file_path: "tests/test_viewer.py" },
          result: "Found the failing assertion.",
          status: "complete",
        },
      ],
    });
    if (folded[3]?.kind !== "tool-run") throw new Error("Expected a tool run");
    expect(folded[3].events).toHaveLength(2);
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

  it("groups adjacent tool calls and routes progress and parent tasks to the matching calls", () => {
    const folded = foldSessionEvents([
      event(1, "assistant", {
        type: "assistant",
        message: {
          content: [
            { type: "tool_use", id: "bash-1", name: "Bash", input: { command: "git status --short" } },
            { type: "tool_use", id: "grep-1", name: "Grep", input: { pattern: "TODO", path: "src" } },
            { type: "tool_use", id: "agent-1", name: "Task", input: { description: "Inspect the callers" } },
          ],
        },
      }),
      event(2, "tool_progress", { type: "tool_progress", tool_use_id: "bash-1", message: "Command is running" }),
      event(3, "system", {
        type: "system",
        subtype: "task_started",
        task_id: "task-agent",
        parent_tool_use_id: "agent-1",
        task_type: "agent",
        description: "Inspect callers",
      }),
      event(4, "system", {
        type: "system",
        subtype: "task_notification",
        task_id: "task-agent",
        parent_tool_use_id: "agent-1",
        status: "completed",
        summary: "Two callers use this helper.",
      }),
    ]);

    expect(folded).toHaveLength(1);
    expect(folded[0]).toMatchObject({
      kind: "tool-run",
      status: "running",
      tools: [{ name: "Bash" }, { name: "Grep" }, { name: "Task" }],
    });
    if (folded[0]?.kind !== "tool-run") throw new Error("Expected a tool run");
    expect(folded[0].tools[0]).toMatchObject({ progress: "Command is running" });
    expect(folded[0].tools[2]?.tasks[0]).toMatchObject({
      title: "Inspect callers",
      detail: "Two callers use this helper.",
      status: "completed",
    });
    expect(folded[0].events.map((item) => item.event_id)).toEqual(["event-1", "event-2", "event-3", "event-4"]);
  });

  it("starts a separate run after an intervening content block and applies standalone tool boundaries", () => {
    const folded = foldSessionEvents([
      event(1, "assistant", {
        type: "assistant",
        message: {
          content: [
            { type: "tool_use", id: "read-1", name: "Read", input: { file_path: "a.ts" } },
            { type: "text", text: "I need your choice." },
            { type: "tool_use", id: "ask-1", name: "AskUserQuestion", input: { question: "Continue?" } },
            { type: "tool_use", id: "grep-1", name: "Grep", input: { pattern: "next", path: "src" } },
          ],
        },
      }),
    ]);
    expect(folded.map((item) => item.kind)).toEqual(["tool-run", "message", "tool-run", "tool-run"]);
    expect(folded[2]).toMatchObject({ kind: "tool-run", standalone: true, tools: [{ name: "AskUserQuestion" }] });
    expect(folded[3]).toMatchObject({ kind: "tool-run", standalone: false, tools: [{ name: "Grep" }] });
  });

  it("turns successful send-to-user calls into assistant prose but preserves denied calls as tools", () => {
    const folded = foldSessionEvents([
      event(1, "assistant", {
        type: "assistant",
        message: {
          content: [
            { type: "tool_use", id: "send-1", name: "SendUserMessage", input: { message: "The deployment is ready." } },
            { type: "tool_use", id: "send-2", name: "SendUserFile", input: { caption: "The patch is attached." } },
          ],
        },
      }),
      event(2, "user", {
        type: "user",
        message: { content: [{ type: "tool_result", tool_use_id: "send-1", content: "Delivered" }] },
      }),
      event(3, "system", {
        type: "system",
        subtype: "permission_denied",
        tool_use_id: "send-2",
        message: "Approval required",
      }),
      event(4, "result", { type: "result" }),
    ]);
    expect(folded[0]).toMatchObject({
      kind: "message",
      role: "assistant",
      text: "The deployment is ready.",
      originToolUseId: "send-1",
    });
    expect(folded[1]).toMatchObject({
      kind: "tool-run",
      standalone: true,
      tools: [{ toolUseId: "send-2", status: "denied", policyDenied: true }],
    });
  });

  it("retains a permission denial that arrives before its tool call", () => {
    const folded = foldSessionEvents([
      event(1, "system", {
        type: "system",
        subtype: "permission_denied",
        tool_use_id: "denied-before-call",
        message: "The command was blocked by policy.",
      }),
      event(2, "assistant", {
        type: "assistant",
        message: {
          content: [
            { type: "tool_use", id: "denied-before-call", name: "Bash", input: { command: "blocked-by-policy" } },
          ],
        },
      }),
    ]);
    expect(folded[0]).toMatchObject({
      kind: "tool-run",
      tools: [{ toolUseId: "denied-before-call", status: "denied", policyDenied: true }],
      events: [{ event_id: "event-1" }, { event_id: "event-2" }],
    });
  });

  it("marks unresolved calls interrupted at turn end and errors failed calls", () => {
    const folded = foldSessionEvents([
      event(1, "assistant", {
        type: "assistant",
        message: {
          content: [
            { type: "tool_use", id: "running", name: "Bash", input: { command: "sleep 10" } },
            { type: "tool_use", id: "failed", name: "Read", input: { file_path: "missing.txt" } },
          ],
        },
      }),
      event(2, "user", {
        type: "user",
        message: {
          content: [{ type: "tool_result", tool_use_id: "failed", is_error: true, content: "File not found" }],
        },
      }),
      event(3, "result", { type: "result" }),
    ]);
    expect(folded[0]).toMatchObject({
      kind: "tool-run",
      status: "error",
      tools: [
        { toolUseId: "running", status: "interrupted" },
        { toolUseId: "failed", status: "error", result: "File not found" },
      ],
    });
  });

  it("keeps unknown event types readable and preserves their source event", () => {
    const [notice] = foldSessionEvents([event(1, "rate_limit_event", { message: "Try again in a moment." })]);
    expect(notice).toMatchObject({ kind: "notice", title: "rate limit event", detail: "Try again in a moment." });
    expect(notice?.events[0]?.event_id).toBe("event-1");
  });
});
