import { describe, expect, it } from "vitest";
import type { ActivityStep, TranscriptRow } from "./transcript-rows";
import { recordPresentation, summarizeActivity, transcriptRows } from "./transcript-rows";
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

describe("archive transcript grouping", () => {
  it("keeps Claude activity records available through fallback steps", () => {
    const prompt = record(1, { type: "user", message: { content: "Inspect the fixture" } });
    const call = record(2, { type: "assistant", message: { content: [{ type: "tool_use", name: "read" }] } });
    const result = record(3, {
      type: "user",
      message: { content: [{ type: "tool_result", content: "fixture output" }] },
    });
    const answer = record(4, {
      type: "assistant",
      message: {
        content: [
          { type: "thinking", thinking: "consider" },
          { type: "text", text: "Fixture answer" },
        ],
      },
    });

    expect(recordPresentation(prompt)).toBe("user");
    expect(recordPresentation(answer)).toBe("assistant");
    expect(transcriptRows([prompt, call, result, answer])).toEqual([
      { kind: "record", records: [prompt] },
      {
        kind: "activity",
        records: [call, result],
        steps: [
          { kind: "record", records: [call] },
          { kind: "record", records: [result] },
        ],
      },
      { kind: "record", records: [answer] },
    ]);
  });

  it("pairs duplicate call IDs FIFO across bookkeeping and a standalone commentary row", () => {
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
    expect(rows[0]).toEqual({ kind: "metadata", records: [bookkeeping] });
    expect(rows[1]).toEqual({ kind: "record", records: [commentary] });
    const activity = activityRow(rows[2]);
    expect(activity.records).toEqual([firstCall, secondCall, firstOutput, secondOutput]);
    expect(activity.steps).toEqual([
      {
        kind: "tool",
        records: [firstCall, firstOutput],
        name: "read",
        input: '{"path":"fixture.txt"}',
        output: '{"stdout":"first","stderr":"warning"}',
        failed: false,
        missingCall: false,
      },
      {
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
    expect(
      rows
        .flatMap((row) => row.records)
        .map((event) => event.ordinal)
        .sort((left, right) => left - right)
    ).toEqual(events.map((event) => event.ordinal));
  });

  it("counts reasoning and agent messages without exposing encrypted content", () => {
    const toolCall = record(1, {
      type: "response_item",
      payload: { type: "function_call", call_id: "tool-1", name: "inspect", arguments: "{}" },
    });
    const firstReasoning = record(2, {
      type: "response_item",
      payload: { type: "reasoning", summary: [{ type: "summary_text", text: "Compare the two values." }] },
    });
    const secondReasoning = record(3, {
      type: "response_item",
      payload: { type: "reasoning", summary: "The values differ." },
    });
    const agentMessage = record(4, {
      type: "response_item",
      payload: {
        type: "agent_message",
        author: "reviewer",
        recipient: "parent",
        content: [
          {
            type: "input_text",
            text: "Message Type: MESSAGE\nTask name: /root\nSender: /root/disclosures\nPayload:\nFound a mismatch.",
          },
        ],
      },
    });
    const encryptedMessage = record(5, {
      type: "response_item",
      payload: {
        type: "agent_message",
        author: "private-agent",
        recipient: "parent",
        content: [
          {
            type: "input_text",
            text: "Message Type: MESSAGE\nTask name: /root\nSender: /root/disclosures\nPayload:\n",
          },
          { type: "encrypted_content", data: "ciphertext" },
        ],
      },
    });
    const output = record(6, {
      type: "response_item",
      payload: { type: "function_call_output", call_id: "tool-1", output: "done" },
    });
    const activity = activityRow(
      transcriptRows([toolCall, firstReasoning, secondReasoning, agentMessage, encryptedMessage, output])[0]
    );

    expect(activity.steps).toEqual([
      { kind: "reasoning", records: [firstReasoning], text: "Compare the two values." },
      { kind: "reasoning", records: [secondReasoning], text: "The values differ." },
      { kind: "subagent", records: [agentMessage], author: "reviewer", recipient: "parent", text: "Found a mismatch." },
      {
        kind: "subagent",
        records: [encryptedMessage],
        author: "private-agent",
        recipient: "parent",
        text: "Message content unavailable",
      },
      {
        kind: "tool",
        records: [toolCall, output],
        name: "inspect",
        input: "{}",
        output: "done",
        failed: false,
        missingCall: false,
      },
    ]);
    expect(summarizeActivity(activity.steps)).toBe("1 tool call, 2 reasoning steps, 2 agent messages");
    const encryptedStep = activity.steps.find(
      (step): step is Extract<ActivityStep, { kind: "subagent" }> =>
        step.kind === "subagent" && step.records[0] === encryptedMessage
    );
    expect(encryptedStep?.text).toBe("Message content unavailable");
    expect(encryptedStep?.text).not.toContain("ciphertext");
    expect(encryptedStep?.records).toEqual([encryptedMessage]);
    expect(activity.steps.find((step) => step.kind === "subagent" && step.records[0] === agentMessage)).toMatchObject({
      kind: "subagent",
      text: "Found a mismatch.",
    });
  });

  it("keeps empty or encrypted-only reasoning as uncounted metadata and shows orphan results honestly", () => {
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
        kind: "activity",
        records: [emptyReasoning, encryptedReasoning, orphan],
        steps: [
          {
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
      { kind: "record", records: [compaction] },
    ]);
    expect(summarizeActivity(activityRow(rows[0]).steps)).toBe("1 tool result");
  });

  it("keeps calls without outputs unfailed and identifies the row by its last record ordinal", () => {
    const call = record(10, {
      type: "response_item",
      payload: { type: "function_call", call_id: "later-output", name: "read", arguments: "{}" },
    });
    const output = record(11, {
      type: "response_item",
      payload: { type: "function_call_output", call_id: "later-output", output: "fixture output" },
    });
    const outputOnly = activityRow(transcriptRows([output])[0]);
    const outputOnlyTool = toolSteps(outputOnly.steps)[0];
    const paired = activityRow(transcriptRows([call, output])[0]);

    expect(outputOnlyTool).toMatchObject({ name: "Tool result", failed: false, missingCall: true });
    expect(outputOnly.records.at(-1)?.ordinal).toBe(output.ordinal);
    expect(paired.records.at(-1)?.ordinal).toBe(output.ordinal);
    expect(toolSteps(paired.steps)).toEqual([
      {
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
    expect(toolSteps(callOnly.steps)[0]).toMatchObject({ output: null, failed: false, missingCall: false });
    expect(summarizeActivity(callOnly.steps)).toBe("1 tool call");
  });

  it("surfaces failed tool outputs and preserves malformed, unknown, and Claude error records", () => {
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
      name: "run",
      output: "command failed",
      failed: true,
      missingCall: false,
    });
    expect(rows.slice(1)).toEqual(events.slice(2).map((event) => ({ kind: "record", records: [event] })));
    expect(
      rows
        .flatMap((row) => row.records)
        .map((event) => event.ordinal)
        .sort((left, right) => left - right)
    ).toEqual(events.map((event) => event.ordinal));
  });
});
