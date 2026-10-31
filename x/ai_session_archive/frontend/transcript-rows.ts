import type { EventRecord } from "./schema";

export type RecordPresentation = "user" | "assistant" | "activity" | "metadata" | "compaction" | "record";

export type ActivityStep =
  | {
      kind: "tool";
      records: EventRecord[];
      name: string;
      input: string | null;
      output: string | null;
      failed: boolean;
      missingCall: boolean;
    }
  | { kind: "reasoning"; records: EventRecord[]; text: string }
  | { kind: "subagent"; records: EventRecord[]; author: string; recipient: string | null; text: string }
  | { kind: "record"; records: EventRecord[] };

export type TranscriptRow =
  | { kind: "record"; records: EventRecord[] }
  | { kind: "metadata"; records: EventRecord[] }
  | { kind: "activity"; records: EventRecord[]; steps: ActivityStep[] };

type ObjectValue = Record<string, unknown>;
type ToolEvent = { kind: "call" | "output"; payload: ObjectValue; callId: string | null };
type ToolPairs = {
  callByOutputOrdinal: Map<number, EventRecord>;
  outputByCallOrdinal: Map<number, EventRecord>;
};

function object(value: unknown): ObjectValue | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as ObjectValue) : null;
}

function string(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function nonemptyString(value: unknown): string | null {
  const candidate = string(value);
  return candidate !== null && candidate.trim().length > 0 ? candidate : null;
}

function responsePayload(record: EventRecord): ObjectValue | null {
  const parsed = object(record.parsed);
  return parsed?.type === "response_item" ? object(parsed.payload) : null;
}

const TOOL_CALL_TYPES = new Set(["function_call", "custom_tool_call"]);
const TOOL_OUTPUT_TYPES = new Set(["function_call_output", "custom_tool_call_output"]);

function toolEvent(record: EventRecord): ToolEvent | null {
  if (record.parse_error != null) return null;
  const payload = responsePayload(record);
  if (payload === null) return null;
  const type = string(payload.type);
  if (type === null) return null;
  if (TOOL_CALL_TYPES.has(type)) {
    return { kind: "call", payload, callId: nonemptyString(payload.call_id) };
  }
  if (TOOL_OUTPUT_TYPES.has(type)) {
    return { kind: "output", payload, callId: nonemptyString(payload.call_id) };
  }
  return null;
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

/** Unknown records and explicit errors remain visible; only known intermediate work is folded. */
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
    const type = string(payload.type);
    if (type !== null && (TOOL_CALL_TYPES.has(type) || TOOL_OUTPUT_TYPES.has(type))) return "activity";
    if (payload.is_error === true) return "record";
    if (type === "message") {
      if (payload.role === "user") return "user";
      if (payload.role === "assistant") return "assistant";
      if (payload.role === "system" || payload.role === "developer") return "metadata";
    }
    if (type === "reasoning") return reasoningText(payload) === null ? "metadata" : "activity";
    if (type === "agent_message") return "activity";
  }
  if (parsed.type === "compacted" || (parsed.type === "event_msg" && payload?.type === "context_compacted"))
    return "compaction";
  if (CLAUDE_METADATA.has(String(parsed.type)) || CODEX_METADATA.has(String(parsed.type))) return "metadata";
  if (parsed.type === "event_msg" && CODEX_BOOKKEEPING.has(String(payload?.type))) return "metadata";
  return "record";
}

/** Groups semantic activity and known metadata while keeping visible messages and unknown records separate. */
export function transcriptRows(records: readonly EventRecord[]): TranscriptRow[] {
  const ordered = [...records].sort((left, right) => left.ordinal - right.ordinal);
  const pairs = pairToolEvents(ordered);
  const rows: TranscriptRow[] = [];

  for (const record of ordered) {
    if (pairs.outputByCallOrdinal.has(record.ordinal)) continue;

    const presentation = recordPresentation(record);
    if (presentation === "metadata") {
      const previous = rows.at(-1);
      if (previous && previous.kind !== "record") previous.records.push(record);
      else rows.push({ kind: "metadata", records: [record] });
      continue;
    }

    if (presentation === "activity") {
      const pairedCall = pairs.callByOutputOrdinal.get(record.ordinal);
      const activityRecords = pairedCall === undefined ? [record] : [pairedCall, record];
      const previous = rows.at(-1);
      if (previous?.kind === "activity") previous.records.push(...activityRecords);
      else if (previous?.kind === "metadata") {
        rows[rows.length - 1] = { kind: "activity", records: [...previous.records, ...activityRecords], steps: [] };
      } else rows.push({ kind: "activity", records: activityRecords, steps: [] });
      continue;
    }

    rows.push({ kind: "record", records: [record] });
  }

  return rows.map((row) =>
    row.kind === "activity"
      ? { ...row, records: sortRecords(row.records), steps: activitySteps(row.records, pairs) }
      : { ...row, records: sortRecords(row.records) }
  );
}

function pairToolEvents(records: readonly EventRecord[]): ToolPairs {
  const waitingCalls = new Map<string, EventRecord[]>();
  const callByOutputOrdinal = new Map<number, EventRecord>();
  const outputByCallOrdinal = new Map<number, EventRecord>();

  for (const record of records) {
    const event = toolEvent(record);
    if (event === null) continue;
    if (event.kind === "call") {
      if (event.callId === null) continue;
      const waiting = waitingCalls.get(event.callId) ?? [];
      waiting.push(record);
      waitingCalls.set(event.callId, waiting);
      continue;
    }

    if (event.callId === null) continue;
    const waiting = waitingCalls.get(event.callId);
    const call = waiting?.shift();
    if (call !== undefined) {
      callByOutputOrdinal.set(record.ordinal, call);
      outputByCallOrdinal.set(call.ordinal, record);
    }
  }

  return { callByOutputOrdinal, outputByCallOrdinal };
}

function activitySteps(records: readonly EventRecord[], pairs: ToolPairs): ActivityStep[] {
  const steps: ActivityStep[] = [];
  const rowOrdinals = new Set(records.map((record) => record.ordinal));

  for (const record of records) {
    const event = toolEvent(record);
    if (event?.kind === "call") {
      const output = pairs.outputByCallOrdinal.get(record.ordinal);
      if (output !== undefined) continue;
      steps.push(toolStep([record], event.payload, null, false));
      continue;
    }
    if (event?.kind === "output") {
      const call = pairs.callByOutputOrdinal.get(record.ordinal);
      if (call !== undefined && rowOrdinals.has(call.ordinal)) {
        const callPayload = toolEvent(call)!.payload;
        steps.push(toolStep([call, record], callPayload, event.payload, false));
      } else {
        steps.push(toolStep([record], event.payload, event.payload, true));
      }
      continue;
    }

    const payload = responsePayload(record);
    if (payload?.type === "reasoning") {
      const text = reasoningText(payload);
      if (text !== null) steps.push({ kind: "reasoning", records: [record], text });
      continue;
    }
    if (payload?.type === "agent_message") {
      steps.push({
        kind: "subagent",
        records: [record],
        author: nonemptyString(payload.author) ?? "Agent",
        recipient: nonemptyString(payload.recipient),
        text: agentMessageText(payload) ?? "Message content unavailable",
      });
      continue;
    }

    if (recordPresentation(record) === "activity") steps.push({ kind: "record", records: [record] });
  }

  return steps.sort((left, right) => lastOrdinal(left.records) - lastOrdinal(right.records));
}

function toolStep(
  records: EventRecord[],
  callPayload: ObjectValue,
  outputPayload: ObjectValue | null,
  missingCall: boolean
): ActivityStep {
  const callType = string(callPayload.type);
  const failed =
    callPayload.is_error === true ||
    callPayload.error != null ||
    outputPayload?.is_error === true ||
    outputPayload?.error != null;
  return {
    kind: "tool",
    records,
    name: missingCall
      ? (nonemptyString(outputPayload?.name) ?? "Tool result")
      : (nonemptyString(callPayload.name) ?? (callType === "custom_tool_call" ? "Custom tool" : "Tool call")),
    input: missingCall ? null : displayFields(callPayload, ["arguments", "input"]),
    output:
      outputPayload === null ? null : displayFields(outputPayload, ["output", "content", "stdout", "stderr", "error"]),
    failed,
    missingCall,
  };
}

function displayFields(value: ObjectValue, keys: readonly string[]): string | null {
  const fields = keys.flatMap((key) =>
    value[key] === undefined || value[key] === null ? [] : [[key, value[key]] as const]
  );
  if (fields.length === 0) return null;
  if (fields.length === 1) return displayValue(fields[0][1]);
  return displayValue(Object.fromEntries(fields));
}

function displayValue(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

function reasoningText(payload: ObjectValue): string | null {
  return plaintext(payload.summary);
}

function agentMessageText(payload: ObjectValue): string | null {
  return agentPlaintext(payload.message) ?? agentPlaintext(payload.content) ?? agentPlaintext(payload.text);
}

const ENCRYPTED_TEXT_TYPES = new Set([
  "encrypted",
  "encrypted_content",
  "redacted",
  "redacted_text",
  "redacted_thinking",
]);
// Codex archives can store the Agentplane routing header as text before encrypted message content.
const AGENT_ROUTING_PREAMBLE =
  /^Message Type: MESSAGE\r?\nTask name: \/root(?:\/[A-Za-z0-9_-]+)*\r?\nSender: \/root(?:\/[A-Za-z0-9_-]+)*\r?\nPayload:\r?\n/;

/** Reads ordinary text fields and deliberately never inspects encrypted-content fields. */
function plaintext(value: unknown): string | null {
  if (typeof value === "string") return value.trim().length === 0 ? null : value;
  if (Array.isArray(value)) {
    const chunks = value.flatMap((item) => {
      const text = plaintext(item);
      return text === null ? [] : [text];
    });
    return chunks.length === 0 ? null : chunks.join("\n");
  }
  const item = object(value);
  if (item === null || ENCRYPTED_TEXT_TYPES.has(String(item.type))) return null;
  if (typeof item.text === "string" && item.text.trim().length > 0) return item.text;
  return plaintext(item.content) ?? plaintext(item.message);
}

function agentPlaintext(value: unknown): string | null {
  if (typeof value === "string") {
    const body = value.replace(AGENT_ROUTING_PREAMBLE, "");
    return body.trim().length === 0 ? null : body;
  }
  if (Array.isArray(value)) {
    const chunks = value.flatMap((item) => {
      const text = agentPlaintext(item);
      return text === null ? [] : [text];
    });
    return chunks.length === 0 ? null : chunks.join("\n");
  }
  const item = object(value);
  if (item === null || ENCRYPTED_TEXT_TYPES.has(String(item.type))) return null;
  if (typeof item.text === "string") return agentPlaintext(item.text);
  return agentPlaintext(item.content) ?? agentPlaintext(item.message);
}

function lastOrdinal(records: readonly EventRecord[]): number {
  return records.reduce((last, record) => Math.max(last, record.ordinal), Number.NEGATIVE_INFINITY);
}

function sortRecords(records: readonly EventRecord[]): EventRecord[] {
  return [...records].sort((left, right) => left.ordinal - right.ordinal);
}

/** Counts semantic steps without presenting an unmatched result as a known tool call. */
export function summarizeActivity(steps: readonly ActivityStep[]): string {
  const calls = steps.filter((step) => step.kind === "tool" && !step.missingCall);
  const results = steps.filter((step) => step.kind === "tool" && step.missingCall);
  const reasoning = steps.filter((step) => step.kind === "reasoning");
  const agents = steps.filter((step) => step.kind === "subagent");
  const records = steps.filter((step) => step.kind === "record");
  const summary: string[] = [];

  if (calls.length > 0) summary.push(countLabel(calls.length, "tool call"));
  if (results.length > 0) summary.push(countLabel(results.length, "tool result"));
  if (reasoning.length > 0) summary.push(countLabel(reasoning.length, "reasoning step"));
  if (agents.length > 0) summary.push(countLabel(agents.length, "agent message"));
  if (records.length > 0) summary.push(countLabel(records.length, "activity record"));
  return summary.join(", ");
}

function countLabel(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}
