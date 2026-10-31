import type { EventRecord } from "./schema";

export type RecordPresentation = "user" | "assistant" | "activity" | "metadata" | "record";
export type TranscriptRow = {
  kind: "record" | "activity" | "metadata";
  records: EventRecord[];
};

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

const CLAUDE_METADATA = new Set([
  "attachment",
  "queue-operation",
  "pr-link",
  "last-prompt",
  "mode",
  "permission-mode",
  "atis-latch",
  "ai-title",
  "file-history-snapshot",
  "file-history-delta",
  "progress",
]);
const CODEX_METADATA = new Set([
  "session_meta",
  "turn_context",
  "token_usage_record",
  "world_state",
  "inter_agent_communication_metadata",
]);
const CODEX_BOOKKEEPING = new Set([
  "token_count",
  "item_completed",
  "task_started",
  "task_complete",
  "thread_settings_applied",
]);

/** Unknown records and explicit failures remain visible; only known intermediate work is folded. */
export function recordPresentation(record: EventRecord): RecordPresentation {
  if (record.parse_error != null) return "record";
  const parsed = object(record.parsed);
  if (!parsed) return "record";
  const message = object(parsed.message);
  if (parsed.type === "user" || parsed.type === "assistant") {
    const content = message?.content;
    if (Array.isArray(content) && content.length > 0) {
      const blocks = content.map(object);
      if (blocks.some((block) => block?.is_error === true)) return "record";
      if (
        blocks.every((block) =>
          ["tool_use", "tool_result", "thinking", "redacted_thinking"].includes(String(block?.type))
        )
      )
        return "activity";
    }
    return parsed.type;
  }
  const payload = object(parsed.payload);
  if (parsed.type === "response_item" && payload) {
    if (payload.type === "message") {
      if (payload.role === "user") return "user";
      if (payload.role === "assistant") return payload.phase === "commentary" ? "activity" : "assistant";
      if (payload.role === "system" || payload.role === "developer") return "metadata";
    }
    if (
      ["reasoning", "function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"].includes(
        String(payload.type)
      )
    )
      return payload.is_error === true ? "record" : "activity";
  }
  if (CLAUDE_METADATA.has(String(parsed.type)) || CODEX_METADATA.has(String(parsed.type))) return "metadata";
  if (parsed.type === "event_msg" && CODEX_BOOKKEEPING.has(String(payload?.type))) return "metadata";
  return "record";
}

/** Groups can span fetched pages; the view keys each group by its last record so prepends retain it. */
export function transcriptRows(records: readonly EventRecord[]): TranscriptRow[] {
  const rows: TranscriptRow[] = [];
  for (const record of records) {
    const presentation = recordPresentation(record);
    const kind = presentation === "activity" || presentation === "metadata" ? presentation : "record";
    const previous = rows.at(-1);
    if (kind !== "record" && previous && previous.kind !== "record") {
      previous.records.push(record);
      if (kind === "activity") previous.kind = "activity";
    } else rows.push({ kind, records: [record] });
  }
  return rows;
}
