import { describe, expect, it } from "vitest";
import type { ActivityStep, TranscriptRow } from "./transcript-rows";
import { isClaudeIntermediateBlock, recordPresentation, summarizeActivity, transcriptRows } from "./transcript-rows";
import type { EventRecord } from "./schema";

function record(ordinal: number, parsed: unknown): EventRecord {
  return { ordinal, parsed, kind: null, timestamp: null, raw_base64: "" };
}

function activityRow(row: TranscriptRow | undefined): Extract<TranscriptRow, { kind: "activity" }> {
  if (row?.kind !== "activity") throw new Error("Expected an activity row");
  return row;
}

function toolSteps(steps: readonly ActivityStep[]): Extract<ActivityStep, { kind: "tool" }>[] {
  return steps.filter((step): step is Extract<ActivityStep, { kind: "tool" }> => step.kind === "tool");
}

function sourceOrdinals(rows: readonly TranscriptRow[]): number[] {
  return [...new Set(rows.flatMap((row) => row.records.map((event) => event.ordinal)))].sort(
    (left, right) => left - right
  );
}

function expectUniqueRecordRefs(records: readonly EventRecord[]): void {
  expect(new Set(records.map((event) => event.ordinal)).size).toBe(records.length);
}

describe("archive transcript grouping", () => {
  it("keeps mixed Claude prose visible and emits each semantic block once", () => {
    const mixed = record(1, {
      type: "assistant",
      message: {
        content: [
          { type: "text", text: "I will compare both files." },
          { type: "tool_use", id: "duplicate", name: "read-first", input: { path: "first.txt" } },
          { type: "tool_use", id: "duplicate", name: "read-second", input: { path: "second.txt" } },
          { type: "tool_result", tool_use_id: "duplicate", content: "first output" },
          { type: "thinking", thinking: "Compare the outputs." },
          { type: "tool_result", tool_use_id: "duplicate", content: "second output", is_error: true },
          { type: "future_block", retained: true },
        ],
      },
    });
    const rows = transcriptRows([mixed]);

    expect(recordPresentation(mixed)).toBe("assistant");
    expect(isClaudeIntermediateBlock({ type: "tool_use" })).toBe(true);
    expect(isClaudeIntermediateBlock({ type: "thinking" })).toBe(true);
    expect(isClaudeIntermediateBlock({ type: "future_block" })).toBe(false);
    expect(rows).toEqual([
      { key: "record:1", kind: "record", records: [mixed] },
      {
        key: "activity:1",
        kind: "activity",
        records: [mixed],
        steps: [
          {
            key: "1:3",
            kind: "tool",
            records: [mixed],
            name: "read-first",
            input: '{"path":"first.txt"}',
            output: "first output",
            failed: false,
            missingCall: false,
          },
          { key: "1:4", kind: "reasoning", records: [mixed], text: "Compare the outputs." },
          {
            key: "1:5",
            kind: "tool",
            records: [mixed],
            name: "read-second",
            input: '{"path":"second.txt"}',
            output: "second output",
            failed: true,
            missingCall: false,
          },
        ],
      },
    ]);
    expect(activityRow(rows[1]).steps.map((step) => step.key)).toEqual(["1:3", "1:4", "1:5"]);
    for (const row of rows) expectUniqueRecordRefs(row.records);
    for (const row of rows) {
      if (row.kind === "activity") {
        for (const step of row.steps) expectUniqueRecordRefs(step.records);
      }
    }
    expect(sourceOrdinals(rows)).toEqual([mixed.ordinal]);
    expect(summarizeActivity(activityRow(rows[1]).steps)).toBe("2 tool calls, 1 reasoning step");
  });

  it("does not pair Claude results with Codex calls that share an ID", () => {
    const codexCall = record(1, {
      type: "response_item",
      payload: { type: "function_call", call_id: "shared-id", name: "shell", arguments: { command: "true" } },
    });
    const claudeResult = record(2, {
      type: "user",
      message: { content: [{ type: "tool_result", tool_use_id: "shared-id", content: "Claude output" }] },
    });
    const codexOutput = record(3, {
      type: "response_item",
      payload: { type: "function_call_output", call_id: "shared-id", output: "Codex output" },
    });
    const rows = transcriptRows([codexCall, claudeResult, codexOutput]);
    const activity = activityRow(rows[0]);

    expect(rows).toEqual([
      {
        key: "activity:3",
        kind: "activity",
        records: [codexCall, claudeResult, codexOutput],
        steps: [
          {
            key: "2:0",
            kind: "tool",
            records: [claudeResult],
            name: "Tool result",
            input: null,
            output: "Claude output",
            failed: false,
            missingCall: true,
          },
          {
            key: "3",
            kind: "tool",
            records: [codexCall, codexOutput],
            name: "shell",
            input: '{"command":"true"}',
            output: "Codex output",
            failed: false,
            missingCall: false,
          },
        ],
      },
    ]);
    expect(summarizeActivity(activity.steps)).toBe("1 tool call, 1 tool result");
    expect(sourceOrdinals(rows)).toEqual([1, 2, 3]);
  });

  it("keeps Claude result keys stable when an older call arrives and marks failed results", () => {
    const call = record(4, {
      type: "assistant",
      message: { content: [{ type: "tool_use", id: "late-call", name: "read", input: { path: "file.txt" } }] },
    });
    const result = record(9, {
      type: "user",
      message: {
        content: [{ type: "tool_result", tool_use_id: "late-call", content: "permission denied", is_error: true }],
      },
    });
    const withoutCall = activityRow(transcriptRows([result])[0]);
    const withCall = activityRow(transcriptRows([call, result])[0]);

    expect(withoutCall.key).toBe("activity:9");
    expect(withCall.key).toBe(withoutCall.key);
    expect(toolSteps(withoutCall.steps)[0]).toMatchObject({
      key: "9:0",
      name: "Tool result",
      failed: true,
      missingCall: true,
    });
    expect(toolSteps(withCall.steps)).toEqual([
      {
        key: "9:0",
        kind: "tool",
        records: [call, result],
        name: "read",
        input: '{"path":"file.txt"}',
        output: "permission denied",
        failed: true,
        missingCall: false,
      },
    ]);
    expect(recordPresentation(result)).toBe("activity");
    expect(withoutCall.kind).toBe("activity");
    expect(withCall.records).toEqual([call, result]);
  });

  it("does not re-emit sibling activity from a call source record in a later result group", () => {
    const source = record(1, {
      type: "assistant",
      message: {
        content: [
          { type: "thinking", thinking: "This belongs to the first group." },
          { type: "tool_use", id: "later-call", name: "read", input: { path: "later.txt" } },
          { type: "tool_result", tool_use_id: "unmatched-call", content: "orphan output" },
        ],
      },
    });
    const commentary = record(2, {
      type: "assistant",
      message: { content: [{ type: "text", text: "I am checking the result." }] },
    });
    const result = record(3, {
      type: "user",
      message: { content: [{ type: "tool_result", tool_use_id: "later-call", content: "read output" }] },
    });
    const rows = transcriptRows([source, commentary, result]);

    expect(rows).toEqual([
      {
        key: "activity:1",
        kind: "activity",
        records: [source],
        steps: [
          { key: "1:0", kind: "reasoning", records: [source], text: "This belongs to the first group." },
          {
            key: "1:2",
            kind: "tool",
            records: [source],
            name: "Tool result",
            input: null,
            output: "orphan output",
            failed: false,
            missingCall: true,
          },
        ],
      },
      { key: "record:2", kind: "record", records: [commentary] },
      {
        key: "activity:3",
        kind: "activity",
        records: [source, result],
        steps: [
          {
            key: "3:0",
            kind: "tool",
            records: [source, result],
            name: "read",
            input: '{"path":"later.txt"}',
            output: "read output",
            failed: false,
            missingCall: false,
          },
        ],
      },
    ]);
    expect(sourceOrdinals(rows)).toEqual([1, 2, 3]);
    for (const row of rows) {
      expectUniqueRecordRefs(row.records);
      if (row.kind === "activity") {
        for (const step of row.steps) expectUniqueRecordRefs(step.records);
      }
    }
  });

  it("leaves empty and redacted Claude thinking as inspectable metadata", () => {
    const emptyThinking = record(1, {
      type: "assistant",
      message: { content: [{ type: "thinking", thinking: "  " }] },
    });
    const redactedThinking = record(2, {
      type: "assistant",
      message: { content: [{ type: "redacted_thinking", data: "opaque" }] },
    });
    const rows = transcriptRows([emptyThinking, redactedThinking]);

    expect(recordPresentation(emptyThinking)).toBe("metadata");
    expect(recordPresentation(redactedThinking)).toBe("metadata");
    expect(rows).toEqual([{ key: "metadata:2", kind: "metadata", records: [emptyThinking, redactedThinking] }]);
    expect(summarizeActivity([])).toBe("");
  });

  it("pairs Codex calls at result positions across metadata and commentary", () => {
    const firstCall = record(1, {
      type: "response_item",
      payload: { type: "function_call", call_id: "same-id", name: "read", arguments: { path: "fixture.txt" } },
    });
    const bookkeeping = record(2, { type: "event_msg", payload: { type: "token_count" } });
    const commentary = record(3, {
      type: "response_item",
      payload: {
        type: "message",
        role: "assistant",
        phase: "commentary",
        content: [{ type: "output_text", text: "I am checking the file." }],
      },
    });
    const secondCall = record(4, {
      type: "response_item",
      payload: { type: "custom_tool_call", call_id: "same-id", name: "write", input: '{"value":2}' },
    });
    const firstOutput = record(5, {
      type: "response_item",
      payload: {
        type: "function_call_output",
        call_id: "same-id",
        output: { stdout: "first", stderr: "warning" },
      },
    });
    const secondOutput = record(6, {
      type: "response_item",
      payload: { type: "custom_tool_call_output", call_id: "same-id", output: ["second", "detail"] },
    });
    const events = [firstCall, bookkeeping, commentary, secondCall, firstOutput, secondOutput];
    const rows = transcriptRows(events);

    expect(recordPresentation(commentary)).toBe("assistant");
    expect(rows.map((row) => row.kind)).toEqual(["metadata", "record", "activity"]);
    expect(rows[0]).toEqual({ key: "metadata:2", kind: "metadata", records: [bookkeeping] });
    expect(rows[1]).toEqual({ key: "record:3", kind: "record", records: [commentary] });
    const activity = activityRow(rows[2]);
    expect(activity.key).toBe("activity:6");
    expect(activity.records).toEqual([firstCall, secondCall, firstOutput, secondOutput]);
    expect(activity.steps).toEqual([
      {
        key: "5",
        kind: "tool",
        records: [firstCall, firstOutput],
        name: "read",
        input: '{"path":"fixture.txt"}',
        output: '{"stdout":"first","stderr":"warning"}',
        failed: false,
        missingCall: false,
      },
      {
        key: "6",
        kind: "tool",
        records: [secondCall, secondOutput],
        name: "write",
        input: '{"value":2}',
        output: '["second","detail"]',
        failed: false,
        missingCall: false,
      },
    ]);
    expect(summarizeActivity(activity.steps)).toBe("2 tool calls");
    expect(sourceOrdinals(rows)).toEqual(events.map((event) => event.ordinal));
  });

  it("counts readable Codex reasoning and agent messages without exposing encrypted content", () => {
    const firstReasoning = record(1, {
      type: "response_item",
      payload: { type: "reasoning", summary: [{ type: "summary_text", text: "Compare the two values." }] },
    });
    const secondReasoning = record(2, {
      type: "response_item",
      payload: { type: "reasoning", summary: "The values differ." },
    });
    const agentMessage = record(3, {
      type: "response_item",
      payload: {
        type: "agent_message",
        author: "reviewer",
        recipient: "parent",
        content: [
          {
            type: "input_text",
            text: "Message Type: MESSAGE\nTask name: /root\nSender: /root/reviewer\nPayload:\nFound a mismatch.",
          },
        ],
      },
    });
    const encryptedMessage = record(4, {
      type: "response_item",
      payload: {
        type: "agent_message",
        author: "private-agent",
        recipient: "parent",
        content: [
          {
            type: "input_text",
            text: "Message Type: MESSAGE\nTask name: /root\nSender: /root/reviewer\nPayload:\n",
          },
          { type: "encrypted_content", data: "ciphertext" },
        ],
      },
    });
    const rows = transcriptRows([firstReasoning, secondReasoning, agentMessage, encryptedMessage]);
    const activity = activityRow(rows[0]);

    expect(activity.steps).toEqual([
      { key: "1", kind: "reasoning", records: [firstReasoning], text: "Compare the two values." },
      { key: "2", kind: "reasoning", records: [secondReasoning], text: "The values differ." },
      {
        key: "3",
        kind: "subagent",
        records: [agentMessage],
        author: "reviewer",
        recipient: "parent",
        text: "Found a mismatch.",
      },
      {
        key: "4",
        kind: "subagent",
        records: [encryptedMessage],
        author: "private-agent",
        recipient: "parent",
        text: "Message content unavailable",
      },
    ]);
    expect(summarizeActivity(activity.steps)).toBe("2 reasoning steps, 2 agent messages");
    expect(
      activity.steps.find((step) => step.kind === "subagent" && step.records[0] === encryptedMessage)
    ).toMatchObject({
      kind: "subagent",
      text: "Message content unavailable",
    });
  });

  it("keeps encrypted-only reasoning uncounted and shows orphan Codex results", () => {
    const emptyReasoning = record(1, { type: "response_item", payload: { type: "reasoning", summary: [] } });
    const encryptedReasoning = record(2, {
      type: "response_item",
      payload: { type: "reasoning", summary: [{ type: "encrypted", text: "ciphertext" }] },
    });
    const orphan = record(3, {
      type: "response_item",
      payload: { type: "function_call_output", call_id: "unseen-call", output: "result text" },
    });
    const compaction = record(4, { type: "event_msg", payload: { type: "context_compacted" } });
    const rows = transcriptRows([emptyReasoning, encryptedReasoning, orphan, compaction]);

    expect(recordPresentation(emptyReasoning)).toBe("metadata");
    expect(recordPresentation(encryptedReasoning)).toBe("metadata");
    expect(recordPresentation(compaction)).toBe("compaction");
    expect(rows).toEqual([
      {
        key: "activity:3",
        kind: "activity",
        records: [emptyReasoning, encryptedReasoning, orphan],
        steps: [
          {
            key: "3",
            kind: "tool",
            records: [orphan],
            name: "Tool result",
            input: null,
            output: "result text",
            failed: false,
            missingCall: true,
          },
        ],
      },
      { key: "record:4", kind: "record", records: [compaction] },
    ]);
    expect(summarizeActivity(activityRow(rows[0]).steps)).toBe("1 tool result");
  });

  it("keeps unmatched Codex calls unfailed and identifies activity rows by their last source ordinal", () => {
    const call = record(10, {
      type: "response_item",
      payload: { type: "function_call", call_id: "later-output", name: "read", arguments: "{}" },
    });
    const output = record(11, {
      type: "response_item",
      payload: { type: "function_call_output", call_id: "later-output", output: "fixture output" },
    });
    const outputOnly = activityRow(transcriptRows([output])[0]);
    const paired = activityRow(transcriptRows([call, output])[0]);

    expect(toolSteps(outputOnly.steps)[0]).toMatchObject({
      key: "11",
      name: "Tool result",
      failed: false,
      missingCall: true,
    });
    expect(outputOnly.key).toBe("activity:11");
    expect(paired.key).toBe("activity:11");
    expect(paired.records.at(-1)?.ordinal).toBe(output.ordinal);
    expect(toolSteps(paired.steps)).toEqual([
      {
        key: "11",
        kind: "tool",
        records: [call, output],
        name: "read",
        input: "{}",
        output: "fixture output",
        failed: false,
        missingCall: false,
      },
    ]);
    const callOnly = activityRow(transcriptRows([call])[0]);
    expect(toolSteps(callOnly.steps)[0]).toMatchObject({
      key: "10",
      output: null,
      failed: false,
      missingCall: false,
    });
    expect(summarizeActivity(callOnly.steps)).toBe("1 tool call");
  });

  it("surfaces Codex errors and keeps malformed or unknown source records inspectable", () => {
    const call = record(1, {
      type: "response_item",
      payload: { type: "custom_tool_call", call_id: "failed", name: "run", input: "{}" },
    });
    const failed = record(2, {
      type: "response_item",
      payload: { type: "custom_tool_call_output", call_id: "failed", is_error: true, error: "command failed" },
    });
    const unknown = record(3, { type: "future_record_type" });
    const malformed = { ...record(4, null), parse_error: "invalid JSON" };
    const claudeError = record(5, {
      type: "user",
      message: { content: [{ type: "tool_result", is_error: true, content: "tool failed" }] },
    });
    const events = [call, failed, unknown, malformed, claudeError];
    const rows = transcriptRows(events);

    expect(toolSteps(activityRow(rows[0]).steps)[0]).toMatchObject({
      key: "2",
      name: "run",
      output: "command failed",
      failed: true,
      missingCall: false,
    });
    expect(rows.slice(1)).toEqual([
      { key: "record:3", kind: "record", records: [unknown] },
      { key: "record:4", kind: "record", records: [malformed] },
      {
        key: "activity:5",
        kind: "activity",
        records: [claudeError],
        steps: [
          {
            key: "5:0",
            kind: "tool",
            records: [claudeError],
            name: "Tool result",
            input: null,
            output: "tool failed",
            failed: true,
            missingCall: true,
          },
        ],
      },
    ]);
    expect(sourceOrdinals(rows)).toEqual(events.map((event) => event.ordinal));
  });

  it("keeps attachments as independent activity steps", () => {
    const attachment = record(8, {
      type: "attachment",
      attachment: { type: "image", filename: "fixture.png", description: "A fixture image" },
    });
    const rows = transcriptRows([attachment]);

    expect(rows).toEqual([
      {
        key: "activity:8",
        kind: "activity",
        records: [attachment],
        steps: [
          {
            key: "8",
            kind: "attachment",
            records: [attachment],
            title: "Attachment · image",
            preview: "fixture.png",
          },
        ],
      },
    ]);
  });
});
