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
  it("keeps event-local fold IDs stable when earlier events are prepended", () => {
    const earlier = event(1, "user", {
      type: "user",
      message: { content: [{ type: "text", text: "Earlier message" }] },
    });
    const later = event(8, "assistant", {
      type: "assistant",
      message: {
        content: [
          { type: "thinking", thinking: "A later event's thinking block" },
          { type: "text", text: "A later event's answer" },
        ],
      },
    });

    const laterItems = foldSessionEvents([later]);
    const fullItems = foldSessionEvents([earlier, later]);

    expect(
      fullItems
        .filter((item) => item.events.some((source) => source.event_id === later.event_id))
        .map((item) => item.id)
    ).toEqual(laterItems.map((item) => item.id));
  });

  it("turns Claude local-command markers into context, stats, usage, and status rows", () => {
    const contextOutput = [
      "## Context Usage",
      "**Model:** claude-sonnet-4-5",
      "**Tokens:** 42.5k / 200k (21%)",
      "### Estimated usage by category",
      "| Category | Tokens |",
      "| --- | ---: |",
      "| Messages | 30k |",
      "| Tools | 12.5k |",
      "### MCP Tools",
      "| Tool | Server | Tokens |",
      "| --- | --- | ---: |",
      "| search | docs | 2.5k |",
      "### Memory Files",
      "| Type | Path | Tokens |",
      "| --- | --- | ---: |",
      "| project | /workspace/README.md | 1k |",
      "### Custom Agents",
      "| Agent | Runs | Tokens |",
      "| --- | ---: | ---: |",
      "| reviewer | 2 | 4k |",
    ].join("\n");
    const folded = foldSessionEvents([
      event(1, "system", {
        type: "system",
        subtype: "local_command_output",
        content: `<local-command-stdout>${contextOutput}</local-command-stdout>`,
      }),
      event(2, "system", {
        type: "system",
        subtype: "local_command_output",
        content:
          '<local-command-stdout><code-stats>{"dailyActivity":[{"date":"2026-09-30","sessionCount":2,"messageCount":12,"toolCallCount":5}]}</code-stats></local-command-stdout>',
      }),
      event(3, "system", {
        type: "system",
        subtype: "local_command_output",
        content: "<local-command-stdout><plan-usage/></local-command-stdout>",
      }),
      event(4, "system", {
        type: "system",
        subtype: "local_command_output",
        content: "<local-command-stdout><session-status/></local-command-stdout>",
      }),
      event(5, "system", {
        type: "system",
        subtype: "local_command_output",
        content: "<local-command-stdout><code-stats></code-stats></local-command-stdout>",
      }),
      event(6, "system", {
        type: "system",
        subtype: "local_command_output",
        content: "<local-command-stdout><code-stats>{broken json}</code-stats></local-command-stdout>",
      }),
      event(7, "system", {
        type: "system",
        subtype: "local_command_output",
        content: "<local-command-stdout>script finished successfully</local-command-stdout>",
      }),
    ]);

    expect(folded.map((item) => item.kind)).toEqual([
      "context",
      "stats",
      "usage",
      "status",
      "stats",
      "message",
      "message",
    ]);
    expect(folded[2]).toMatchObject({ kind: "usage", events: [{ event_id: "event-3" }] });
    expect(folded[3]).toMatchObject({ kind: "status", events: [{ event_id: "event-4" }] });
    expect(folded[0]).toMatchObject({
      kind: "context",
      model: "claude-sonnet-4-5",
      totalTokens: 42_500,
      rawMaxTokens: 200_000,
      percentage: 21,
      categories: [
        { name: "Messages", tokens: 30_000 },
        { name: "Tools", tokens: 12_500 },
      ],
      mcpTools: [{ name: "search", serverName: "docs", tokens: 2_500 }],
      memoryFiles: [{ type: "project", path: "/workspace/README.md", tokens: 1_000 }],
      agents: [{ agentType: "reviewer", tokens: 4_000 }],
    });
    expect(folded[1]).toMatchObject({
      kind: "stats",
      stats: { dailyActivity: [{ date: "2026-09-30", sessionCount: 2, messageCount: 12, toolCallCount: 5 }] },
    });
    expect(folded[4]).toMatchObject({ kind: "stats", stats: null });
    expect(folded[5]).toMatchObject({
      kind: "message",
      role: "assistant",
      text: "<code-stats>{broken json}</code-stats>",
    });
    expect(folded[6]).toMatchObject({ kind: "message", role: "assistant", text: "script finished successfully" });
  });

  it("renders peer messages and folds held, released, and dropped message states", () => {
    const folded = foldSessionEvents([
      event(1, "system", {
        type: "system",
        subtype: "peer_message_hold",
        message_uuid: "peer-message-1",
        from: "agent-17",
        from_name: "Review agent",
        state: "held",
        cause: "mode-mismatch",
      }),
      event(2, "user", {
        type: "user",
        uuid: "peer-message-1",
        origin: {
          kind: "peer",
          from: "agent-17",
          name: "Review agent",
          handback: true,
          handbackNote: "I checked the active assertion and found the fixture mismatch.",
        },
        message: { content: [{ type: "text", text: "The test expects active, but the fixture says paused." }] },
      }),
      event(3, "system", {
        type: "system",
        subtype: "peer_message_hold",
        message_uuid: "peer-message-1",
        state: "released",
      }),
      event(4, "system", {
        type: "system",
        subtype: "peer_message_hold",
        message_uuid: "peer-message-2",
        from: "agent-42",
        from_name: "Build agent",
        state: "held",
        cause: "no-mode-asserted",
      }),
      event(5, "system", {
        type: "system",
        subtype: "peer_message_hold",
        message_uuid: "peer-message-2",
        state: "dropped",
        outcome: "expired",
      }),
      event(6, "system", {
        type: "system",
        subtype: "peer_message_hold",
        message_uuid: "invalid-held-message",
        state: "held",
      }),
    ]);

    expect(folded.map((item) => item.kind)).toEqual(["peer-message", "peer-hold"]);
    expect(folded[0]).toMatchObject({
      kind: "peer-message",
      messageUuid: "peer-message-1",
      from: "agent-17",
      name: "Review agent",
      handback: true,
      handbackNote: "I checked the active assertion and found the fixture mismatch.",
      text: "The test expects active, but the fixture says paused.",
      events: [{ event_id: "event-1" }, { event_id: "event-2" }, { event_id: "event-3" }],
    });
    expect(folded[1]).toMatchObject({
      kind: "peer-hold",
      messageUuid: "peer-message-2",
      from: "agent-42",
      name: "Build agent",
      state: "dropped",
      cause: "no-mode-asserted",
      outcome: "expired",
      events: [{ event_id: "event-4" }, { event_id: "event-5" }],
    });
  });

  it("keeps a held peer row and applies release even when the peer message arrives later", () => {
    const [hold] = foldSessionEvents([
      event(1, "system", {
        type: "system",
        subtype: "peer_message_hold",
        message_uuid: "pending-message",
        from: "agent-17",
        state: "held",
        cause: "mode-mismatch",
      }),
    ]);
    expect(hold).toMatchObject({ kind: "peer-hold", state: "held", from: "agent-17", cause: "mode-mismatch" });

    const released = foldSessionEvents([
      event(1, "system", {
        type: "system",
        subtype: "peer_message_hold",
        message_uuid: "pending-message",
        from: "agent-17",
        state: "held",
      }),
      event(2, "system", {
        type: "system",
        subtype: "peer_message_hold",
        message_uuid: "pending-message",
        state: "released",
      }),
      event(3, "user", {
        type: "user",
        uuid: "pending-message",
        origin: { kind: "peer", from: "agent-17", name: "Review agent" },
        message: { content: [{ type: "text", text: "The review is complete." }] },
      }),
    ]);
    expect(released).toMatchObject([
      {
        kind: "peer-message",
        messageUuid: "pending-message",
        events: [{ event_id: "event-1" }, { event_id: "event-2" }, { event_id: "event-3" }],
      },
    ]);
    expect(released[0]).not.toHaveProperty("handback");
    expect(released[0]).not.toHaveProperty("handbackNote");
  });

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

  it("suppresses ambient and skip_transcript task events and their later notifications", () => {
    const folded = foldSessionEvents([
      event(1, "system", {
        type: "system",
        subtype: "task_started",
        task_id: "hidden-task",
        description: "Internal background work",
        skip_transcript: true,
      }),
      event(2, "system", {
        type: "system",
        subtype: "task_notification",
        task_id: "hidden-task",
        status: "completed",
        summary: "Large hidden internal summary",
      }),
      event(3, "system", {
        type: "system",
        subtype: "task_started",
        task_id: "ambient-task",
        description: "Ambient housekeeping",
        ambient: true,
      }),
      event(4, "system", {
        type: "system",
        subtype: "task_notification",
        task_id: "ambient-task",
        status: "completed",
        summary: "Large hidden ambient summary",
      }),
    ]);

    expect(folded).toEqual([]);
  });

  it("folds child tool activity into its parent agent without rendering child rows", () => {
    const folded = foldSessionEvents([
      event(1, "assistant", {
        type: "assistant",
        message: {
          content: [
            {
              type: "tool_use",
              id: "agent-1",
              name: "Task",
              input: { description: "Check the failing test" },
            },
          ],
        },
      }),
      event(2, "assistant", {
        type: "assistant",
        parent_tool_use_id: "agent-1",
        message: {
          model: "claude-sonnet-4-5-20250929",
          content: [
            { type: "tool_use", id: "agent-read", name: "Read", input: { file_path: "test_viewer.py" } },
            { type: "tool_use", id: "agent-grep", name: "Grep", input: { pattern: "status", path: "tests" } },
            { type: "text", text: "The fixture and assertion disagree." },
          ],
        },
      }),
      event(3, "user", {
        type: "user",
        parent_tool_use_id: "agent-1",
        message: {
          content: [{ type: "tool_result", tool_use_id: "agent-read", content: "HIDDEN_CHILD_TOOL_OUTPUT" }],
        },
      }),
    ]);

    expect(folded[0]).toMatchObject({
      kind: "tool-run",
      tools: [
        {
          toolUseId: "agent-1",
          name: "Task",
          status: "running",
          subagentActivity: {
            latestToolName: "Grep",
            toolCallCount: 2,
            model: "claude-sonnet-4-5-20250929",
          },
        },
      ],
    });
    expect(folded).toHaveLength(1);
    expect(folded.map((item) => item.kind)).toEqual(["tool-run"]);
  });

  it("renders only a parentless sidechain explicitly left in the transcript", () => {
    const folded = foldSessionEvents([
      event(1, "assistant", {
        type: "assistant",
        isSidechain: true,
        message: { content: [{ type: "text", text: "Internal sidechain text." }] },
      }),
      event(2, "assistant", {
        type: "assistant",
        isSidechain: true,
        subagentRowLeftToParent: true,
        message: { content: [{ type: "text", text: "Handed back to the parent." }] },
      }),
    ]);

    expect(folded).toMatchObject([{ kind: "message", text: "Handed back to the parent." }]);
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

  it("folds successful Read output into a cleaned file preview", () => {
    const rawOutput =
      "1: const answer = 42;\n2: console.log(answer);\n\n<system-reminder>fixture-only reminder</system-reminder>";
    const folded = foldSessionEvents([
      event(1, "assistant", {
        type: "assistant",
        message: {
          content: [{ type: "tool_use", id: "read-file", name: "Read", input: { file_path: "src/example.ts" } }],
        },
      }),
      event(2, "user", {
        type: "user",
        message: {
          content: [{ type: "tool_result", tool_use_id: "read-file", content: rawOutput }],
        },
      }),
    ]);

    const tool = folded[0]?.kind === "tool-run" ? folded[0].tools[0] : undefined;
    expect(tool).toMatchObject({
      result: rawOutput,
      filePreview: { path: "src/example.ts", contents: "const answer = 42;\nconsole.log(answer);" },
    });
  });

  it("preserves unnumbered Read text and does not build previews for errors", () => {
    const folded = foldSessionEvents([
      event(1, "assistant", {
        type: "assistant",
        message: {
          content: [
            { type: "tool_use", id: "read-plain", name: "Read", input: { file_path: "README.md" } },
            { type: "tool_use", id: "read-error", name: "Read", input: { file_path: "missing.txt" } },
          ],
        },
      }),
      event(2, "user", {
        type: "user",
        message: {
          content: [
            { type: "tool_result", tool_use_id: "read-plain", content: "# heading\n1: mixed line\n" },
            { type: "tool_result", tool_use_id: "read-error", is_error: true, content: "File not found" },
          ],
        },
      }),
    ]);

    if (folded[0]?.kind !== "tool-run") throw new Error("Expected a tool run");
    expect(folded[0].tools[0]).toMatchObject({
      result: "# heading\n1: mixed line\n",
      filePreview: { path: "README.md", contents: "# heading\n1: mixed line" },
    });
    expect(folded[0].tools[1]).toMatchObject({ result: "File not found", failed: true });
    expect(folded[0].tools[1]).not.toHaveProperty("filePreview");
  });

  it("keeps result text, images, and tool-use metadata as separate fields", () => {
    const toolUseResult = {
      tool_use_id: "read-image",
      structuredContent: { contentType: "screenshot", secretMetadata: "not transcript output" },
    };
    const folded = foldSessionEvents([
      event(1, "assistant", {
        type: "assistant",
        message: {
          content: [{ type: "tool_use", id: "read-image", name: "Read", input: { file_path: "diagram.png" } }],
        },
      }),
      event(2, "user", {
        type: "user",
        tool_use_result: toolUseResult,
        message: {
          content: [
            {
              type: "tool_result",
              tool_use_id: "read-image",
              content: [
                { type: "text", text: "A diagram is attached." },
                { type: "image", source: { type: "base64", media_type: "image/png", data: "c3ludGhldGlj" } },
              ],
            },
          ],
        },
      }),
    ]);

    expect(folded[0]).toMatchObject({
      kind: "tool-run",
      tools: [
        {
          name: "Read",
          result: "A diagram is attached.",
          outputImages: [{ data: "c3ludGhldGlj", mimeType: "image/png" }],
          toolUseResult,
          status: "complete",
        },
      ],
    });
    if (folded[0]?.kind !== "tool-run") throw new Error("Expected a tool run");
    expect(folded[0].tools[0]?.result).not.toContain("not transcript output");
  });

  it("omits hook output, runner logs, and unsupported event types from the default fold", () => {
    const folded = foldSessionEvents([
      event(1, "user", { type: "user", message: { content: [{ type: "text", text: "Please inspect the project." }] } }),
      event(2, "system", {
        type: "system",
        subtype: "hook_started",
        hook_name: "PostToolUse",
        description: "Large hook lifecycle detail that Claude Web does not show.",
      }),
      event(3, "system", {
        type: "system",
        subtype: "hook_response",
        response: { stdout: "large hook output that should not become a transcript row" },
      }),
      event(4, "env_manager_log", {
        type: "env_manager_log",
        data: { level: "info", message: "large environment-manager details that are not transcript content" },
      }),
      event(5, "rate_limit_event", { type: "rate_limit_event", message: "Unsupported event kind" }),
      event(6, "assistant", {
        type: "assistant",
        message: { content: [{ type: "text", text: "The project is ready." }] },
      }),
    ]);

    expect(folded.map((item) => item.kind)).toEqual(["message", "message"]);
    expect(folded.map((item) => (item.kind === "message" ? item.text : ""))).toEqual([
      "Please inspect the project.",
      "The project is ready.",
    ]);
  });

  it("omits unsupported content blocks and folds connector text as a message", () => {
    const folded = foldSessionEvents([
      event(1, "assistant", {
        type: "assistant",
        message: {
          content: [
            { type: "connector_text", text: "Connected source result." },
            { type: "vendor_internal_blob", text: "Large hidden internal payload." },
            { type: "text", text: "No response requested." },
          ],
        },
      }),
    ]);

    expect(folded).toMatchObject([{ kind: "message", text: "Connected source result." }]);
  });
});
