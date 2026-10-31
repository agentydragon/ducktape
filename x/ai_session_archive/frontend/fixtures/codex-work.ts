import type { EventRecord } from "../schema";

// Sanitized record shapes from the transcript UI extraction session. No source paths,
// credentials, encrypted bodies or actual command output are copied from that session.
const sourceRecords: unknown[] = [
  { type: "session_meta", payload: { id: "fixture-work-session" } },
  {
    type: "response_item",
    payload: {
      type: "message",
      role: "user",
      content: [{ type: "input_text", text: "Make the archive transcript easier to read." }],
    },
  },
  {
    type: "response_item",
    payload: {
      type: "message",
      role: "assistant",
      phase: "commentary",
      content: [{ type: "output_text", text: "I'll compare the components and extract the compact step layout." }],
    },
  },
  { type: "response_item", payload: { type: "reasoning", summary: [], encrypted_content: "[omitted]" } },
  {
    type: "response_item",
    payload: {
      type: "custom_tool_call",
      call_id: "inspect",
      name: "functions.exec",
      input: "text(await tools.exec_command({cmd: 'rg -n StepLine util/frontend'}));",
    },
  },
  { type: "event_msg", payload: { type: "item_completed", item_id: "inspect" } },
  { type: "token_usage_record", payload: { total_tokens: 1234 } },
  {
    type: "response_item",
    payload: {
      type: "custom_tool_call_output",
      call_id: "inspect",
      output: "Found the compact step component and its disclosure styles.",
    },
  },
  { type: "inter_agent_communication_metadata", payload: { recipient: "/root" } },
  {
    type: "response_item",
    payload: {
      type: "agent_message",
      author: "/root/shared_steps",
      recipient: "/root",
      content: [
        {
          type: "input_text",
          text: "The shared step layout is ready. Its protocol-specific labels remain in each application.",
        },
      ],
    },
  },
  {
    type: "response_item",
    payload: {
      type: "message",
      role: "assistant",
      phase: "commentary",
      content: [
        { type: "output_text", text: "The components are shared. I'm checking the archive's grouping and pagination." },
      ],
    },
  },
  {
    type: "response_item",
    payload: {
      type: "reasoning",
      summary: [{ type: "summary_text", text: "Pair tool calls by their recorded call ID." }],
    },
  },
  { type: "event_msg", payload: { type: "token_count" } },
  {
    type: "response_item",
    payload: {
      type: "reasoning",
      summary: [{ type: "summary_text", text: "Keep commentary outside the work group so progress remains readable." }],
    },
  },
  {
    type: "response_item",
    payload: {
      type: "function_call",
      call_id: "check",
      name: "run_checks",
      arguments: '{"targets":["transcript_rows_test","session_viewer_test"]}',
    },
  },
  {
    type: "response_item",
    payload: {
      type: "function_call_output",
      call_id: "check",
      output: Array.from(
        { length: 48 },
        (_, index) => `PASS check ${String(index + 1).padStart(2, "0")}: captured transcript row`
      ).join("\n"),
    },
  },
  { type: "compacted", payload: { summary: "[omitted compaction context]" } },
  {
    type: "response_item",
    payload: {
      type: "message",
      role: "assistant",
      phase: "final_answer",
      content: [
        {
          type: "output_text",
          text: "The archive now uses **compact work steps**. Commentary stays visible, tool arguments and results open together, and the original records remain inspectable.",
        },
      ],
    },
  },
  { type: "event_msg", payload: { type: "task_complete" } },
];

export const codexWorkRecords: EventRecord[] = sourceRecords.map((parsed, index) => ({
  ordinal: index + 1,
  parsed,
  kind: null,
  timestamp: "2026-10-31T06:00:00Z",
  raw_base64: btoa(JSON.stringify(parsed)),
}));
