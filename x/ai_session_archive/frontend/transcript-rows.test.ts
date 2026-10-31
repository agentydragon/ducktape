import { describe, expect, it } from "vitest";
import type { EventRecord } from "./schema";
import { recordPresentation, transcriptRows } from "./transcript-rows";

function record(ordinal: number, parsed: unknown): EventRecord {
  return { ordinal, parsed, kind: null, timestamp: null, raw_base64: "" };
}

describe("archive transcript grouping", () => {
  it("folds Claude tool-result user records while preserving actual prompts and mixed assistant answers", () => {
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
      { kind: "activity", records: [call, result] },
      { kind: "record", records: [answer] },
    ]);
  });

  it("keeps interleaved Codex bookkeeping inside one work sequence, bounded by the final answer", () => {
    const events = [
      record(1, { type: "response_item", payload: { type: "reasoning" } }),
      record(2, { type: "event_msg", payload: { type: "token_count" } }),
      record(3, { type: "response_item", payload: { type: "message", role: "assistant", phase: "commentary" } }),
      record(4, { type: "response_item", payload: { type: "custom_tool_call" } }),
      record(5, { type: "response_item", payload: { type: "custom_tool_call_output" } }),
      record(6, { type: "response_item", payload: { type: "message", role: "assistant", phase: "final_answer" } }),
    ];
    expect(transcriptRows(events)).toEqual([
      { kind: "activity", records: events.slice(0, 5) },
      { kind: "record", records: events.slice(5) },
    ]);
  });

  it("preserves malformed, unknown, and explicit failure records as visible boundaries", () => {
    const metadata = record(1, { type: "session_meta" });
    const malformed = { ...record(2, null), parse_error: "invalid JSON" };
    const unknown = record(3, { type: "future_record_type" });
    const failedTool = record(4, { type: "user", message: { content: [{ type: "tool_result", is_error: true }] } });
    const events = [metadata, malformed, unknown, failedTool];
    expect(transcriptRows(events)).toEqual([
      { kind: "metadata", records: [metadata] },
      ...events.slice(1).map((event) => ({ kind: "record", records: [event] })),
    ]);
  });

  it("extends a sequence across page boundaries without mutating records or losing their order", () => {
    const head = record(10, { type: "response_item", payload: { type: "function_call" } });
    const tail = record(11, { type: "response_item", payload: { type: "function_call_output" } });
    const firstPage = Object.freeze([head]);
    const previous = transcriptRows(firstPage);
    const appended = transcriptRows([...firstPage, tail]);
    expect(appended[0].records[0].ordinal).toBe(previous[0].records[0].ordinal);
    expect(appended).toEqual([{ kind: "activity", records: [head, tail] }]);
    expect(previous[0].records).toEqual([head]);
  });
});
