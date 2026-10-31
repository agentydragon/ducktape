import type { EventRecord } from "./schema";

export type RecordPresentation = "user" | "assistant" | "activity" | "metadata" | "compaction" | "record";

export type ActivityStep =
  | {
      key: string;
      kind: "tool";
      records: EventRecord[];
      name: string;
      input: string | null;
      output: string | null;
      failed: boolean;
      missingCall: boolean;
    }
  | { key: string; kind: "reasoning"; records: EventRecord[]; text: string }
  | { key: string; kind: "subagent"; records: EventRecord[]; author: string; recipient: string | null; text: string }
  | { key: string; kind: "attachment"; records: EventRecord[]; title: string; preview: string }
  | { key: string; kind: "record"; records: EventRecord[] };

export type TranscriptRow =
  | { key: string; kind: "record"; records: EventRecord[] }
  | { key: string; kind: "metadata"; records: EventRecord[] }
  | { key: string; kind: "activity"; records: EventRecord[]; steps: ActivityStep[] };

type ObjectValue = Record<string, unknown>;
type ToolNamespace = "claude" | "codex";
type ToolEvent = {
  key: string;
  namespace: ToolNamespace;
  kind: "call" | "output";
  payload: ObjectValue;
  callId: string | null;
  record: EventRecord;
  blockIndex: number;
};
type ToolPairs = {
  callByOutputKey: Map<string, ToolEvent>;
  outputByCallKey: Map<string, ToolEvent>;
};
type StepCandidate = { step: ActivityStep; ordinal: number; blockIndex: number };
type MutableTranscriptRow =
  | { kind: "record"; records: EventRecord[] }
  | { kind: "metadata"; records: EventRecord[] }
  | { kind: "activity"; records: EventRecord[]; stepRecords: EventRecord[] };

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

function claudeContent(record: EventRecord): unknown[] | null {
  if (record.parse_error != null) return null;
  const parsed = object(record.parsed);
  if (parsed?.type !== "user" && parsed?.type !== "assistant") return null;
  const content = object(parsed.message)?.content;
  return Array.isArray(content) ? content : null;
}

const CLAUDE_INTERMEDIATE_TYPES = new Set(["tool_use", "tool_result", "thinking", "redacted_thinking"]);

/** Identifies Claude blocks that the transcript renderer presents as activity instead of message prose. */
export function isClaudeIntermediateBlock(value: unknown): boolean {
  const block = object(value);
  return block !== null && CLAUDE_INTERMEDIATE_TYPES.has(String(block.type));
}

function claudeThinkingText(block: ObjectValue): string | null {
  const text = string(block.thinking);
  return text !== null && text.trim().length > 0 ? text : null;
}

const TOOL_CALL_TYPES = new Set(["function_call", "custom_tool_call"]);
const TOOL_OUTPUT_TYPES = new Set(["function_call_output", "custom_tool_call_output"]);

function toolEvents(record: EventRecord): ToolEvent[] {
  if (record.parse_error != null) return [];
  const parsed = object(record.parsed);
  if (parsed?.type === "user" || parsed?.type === "assistant") {
    const content = claudeContent(record);
    if (content === null) return [];
    return content.flatMap<ToolEvent>((value, blockIndex) => {
      const payload = object(value);
      const type = string(payload?.type);
      if (payload === null || type === null) return [];
      if (type === "tool_use") {
        return [
          {
            key: record.ordinal + ":" + blockIndex,
            namespace: "claude" as const,
            kind: "call" as const,
            payload,
            callId: nonemptyString(payload.id),
            record,
            blockIndex,
          },
        ];
      }
      if (type === "tool_result") {
        return [
          {
            key: record.ordinal + ":" + blockIndex,
            namespace: "claude" as const,
            kind: "output" as const,
            payload,
            callId: nonemptyString(payload.tool_use_id),
            record,
            blockIndex,
          },
        ];
      }
      return [];
    });
  }

  const payload = responsePayload(record);
  const type = string(payload?.type);
  if (payload === null || type === null) return [];
  if (TOOL_CALL_TYPES.has(type)) {
    return [
      {
        key: String(record.ordinal),
        namespace: "codex",
        kind: "call",
        payload,
        callId: nonemptyString(payload.call_id),
        record,
        blockIndex: 0,
      },
    ];
  }
  if (TOOL_OUTPUT_TYPES.has(type)) {
    return [
      {
        key: String(record.ordinal),
        namespace: "codex",
        kind: "output",
        payload,
        callId: nonemptyString(payload.call_id),
        record,
        blockIndex: 0,
      },
    ];
  }
  return [];
}

const CLAUDE_METADATA = new Set([
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
  if (parsed.type === "attachment" && object(parsed.attachment) !== null) return "activity";
  if (parsed.type === "user" || parsed.type === "assistant") {
    const content = message?.content;
    if (Array.isArray(content) && content.length > 0 && content.every(isClaudeIntermediateBlock)) {
      const hasToolBlock = content.some((block) => {
        const type = string(object(block)?.type);
        return type === "tool_use" || type === "tool_result";
      });
      const hasReadableThinking = content.some((block) => {
        const item = object(block);
        return item?.type === "thinking" && claudeThinkingText(item) !== null;
      });
      return hasToolBlock || hasReadableThinking ? "activity" : "metadata";
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
  const rows: MutableTranscriptRow[] = [];

  for (const record of ordered) {
    const presentation = recordPresentation(record);
    const activityRecords = activityContribution(record, pairs);

    if (presentation === "metadata" && activityRecords === null) {
      const previous = rows.at(-1);
      if (previous && previous.kind !== "record") previous.records = uniqueRecords([...previous.records, record]);
      else rows.push({ kind: "metadata", records: [record] });
      continue;
    }

    if (presentation !== "activity" && presentation !== "metadata") {
      rows.push({ kind: "record", records: [record] });
    }

    if (activityRecords !== null) appendActivity(rows, activityRecords, record);
  }

  return rows.map((row): TranscriptRow => {
    const sorted = sortRecords(row.records);
    const key = row.kind + ":" + sorted.at(-1)!.ordinal;
    return row.kind === "activity"
      ? { key, kind: "activity", records: sorted, steps: activitySteps(row.stepRecords, sorted, pairs) }
      : { key, kind: row.kind, records: sorted };
  });
}

function appendActivity(rows: MutableTranscriptRow[], records: readonly EventRecord[], stepRecord: EventRecord): void {
  const previous = rows.at(-1);
  if (previous?.kind === "activity") {
    previous.records = uniqueRecords([...previous.records, ...records]);
    previous.stepRecords = uniqueRecords([...previous.stepRecords, stepRecord]);
  } else if (previous?.kind === "metadata") {
    rows[rows.length - 1] = {
      kind: "activity",
      records: uniqueRecords([...previous.records, ...records]),
      stepRecords: [stepRecord],
    };
  } else {
    rows.push({ kind: "activity", records: uniqueRecords(records), stepRecords: [stepRecord] });
  }
}

function activityContribution(record: EventRecord, pairs: ToolPairs): EventRecord[] | null {
  const events = toolEvents(record);
  const emittedEvents = events.filter((event) => event.kind === "output" || !pairs.outputByCallKey.has(event.key));
  const thinking = claudeThinkingEntries(record);
  const parsed = object(record.parsed);
  const attachment = parsed?.type === "attachment" && record.parse_error == null ? object(parsed.attachment) : null;
  const payload = responsePayload(record);
  const hasOtherActivity =
    attachment !== null ||
    (payload?.type === "reasoning" && reasoningText(payload) !== null) ||
    payload?.type === "agent_message";
  if (emittedEvents.length === 0 && thinking.length === 0 && !hasOtherActivity) return null;

  const sourceRecords = [record];
  for (const event of emittedEvents) {
    if (event.kind !== "output") continue;
    const call = pairs.callByOutputKey.get(event.key);
    if (call !== undefined) sourceRecords.push(call.record);
  }
  return uniqueRecords(sourceRecords);
}

function pairToolEvents(records: readonly EventRecord[]): ToolPairs {
  const waitingCalls = new Map<string, ToolEvent[]>();
  const callByOutputKey = new Map<string, ToolEvent>();
  const outputByCallKey = new Map<string, ToolEvent>();

  for (const record of records) {
    for (const event of toolEvents(record)) {
      if (event.callId === null) continue;
      const queueKey = event.namespace + "\u0000" + event.callId;
      if (event.kind === "call") {
        const waiting = waitingCalls.get(queueKey) ?? [];
        waiting.push(event);
        waitingCalls.set(queueKey, waiting);
        continue;
      }

      const call = waitingCalls.get(queueKey)?.shift();
      if (call !== undefined) {
        callByOutputKey.set(event.key, call);
        outputByCallKey.set(call.key, event);
      }
    }
  }

  return { callByOutputKey, outputByCallKey };
}

function activitySteps(
  stepRecords: readonly EventRecord[],
  sourceRecords: readonly EventRecord[],
  pairs: ToolPairs
): ActivityStep[] {
  const candidates: StepCandidate[] = [];
  const sourceOrdinals = new Set(sourceRecords.map((record) => record.ordinal));

  for (const record of stepRecords) {
    const parsed = object(record.parsed);
    const attachment = parsed?.type === "attachment" ? object(parsed.attachment) : null;
    if (attachment !== null && record.parse_error == null) {
      candidates.push({
        ordinal: record.ordinal,
        blockIndex: 0,
        step: {
          key: String(record.ordinal),
          kind: "attachment",
          records: [record],
          title: "Attachment · " + (string(attachment.type) ?? "unknown").replaceAll("_", " "),
          preview:
            string(attachment.displayPath) ??
            string(attachment.filename) ??
            string(attachment.hookName) ??
            string(attachment.description) ??
            "",
        },
      });
      continue;
    }

    const content = claudeContent(record);
    if (content !== null) {
      const eventsByIndex = new Map(toolEvents(record).map((event) => [event.blockIndex, event]));
      content.forEach((value, blockIndex) => {
        const block = object(value);
        if (block === null) return;
        const event = eventsByIndex.get(blockIndex);
        if (event !== undefined) {
          if (event.kind === "call") {
            if (!pairs.outputByCallKey.has(event.key)) candidates.push(toolCandidate(event, null));
            return;
          }
          const call = pairs.callByOutputKey.get(event.key);
          if (call === undefined || !sourceOrdinals.has(call.record.ordinal)) {
            candidates.push(toolCandidate(event, null));
          } else {
            candidates.push(toolCandidate(event, call));
          }
          return;
        }

        if (block.type === "thinking") {
          const text = claudeThinkingText(block);
          if (text !== null) {
            candidates.push({
              ordinal: record.ordinal,
              blockIndex,
              step: { key: record.ordinal + ":" + blockIndex, kind: "reasoning", records: [record], text },
            });
          }
        }
      });
      continue;
    }

    const event = toolEvents(record)[0];
    if (event !== undefined) {
      if (event.kind === "call") {
        if (!pairs.outputByCallKey.has(event.key)) candidates.push(toolCandidate(event, null));
      } else {
        const call = pairs.callByOutputKey.get(event.key);
        if (call === undefined || !sourceOrdinals.has(call.record.ordinal)) {
          candidates.push(toolCandidate(event, null));
        } else {
          candidates.push(toolCandidate(event, call));
        }
      }
      continue;
    }

    const payload = responsePayload(record);
    if (payload?.type === "reasoning") {
      const text = reasoningText(payload);
      if (text !== null) {
        candidates.push({
          ordinal: record.ordinal,
          blockIndex: 0,
          step: { key: String(record.ordinal), kind: "reasoning", records: [record], text },
        });
      }
      continue;
    }
    if (payload?.type === "agent_message") {
      candidates.push({
        ordinal: record.ordinal,
        blockIndex: 0,
        step: {
          key: String(record.ordinal),
          kind: "subagent",
          records: [record],
          author: nonemptyString(payload.author) ?? "Agent",
          recipient: nonemptyString(payload.recipient),
          text: agentMessageText(payload) ?? "Message content unavailable",
        },
      });
      continue;
    }

    if (recordPresentation(record) === "activity") {
      candidates.push({
        ordinal: record.ordinal,
        blockIndex: 0,
        step: { key: String(record.ordinal), kind: "record", records: [record] },
      });
    }
  }

  return candidates
    .sort((left, right) => left.ordinal - right.ordinal || left.blockIndex - right.blockIndex)
    .map((candidate) => candidate.step);
}

function toolCandidate(event: ToolEvent, call: ToolEvent | null): StepCandidate {
  const records = uniqueRecords(call === null ? [event.record] : [call.record, event.record]);
  const step =
    event.namespace === "claude" ? claudeToolStep(event, call, records) : codexToolStep(event, call, records);
  return { step, ordinal: event.record.ordinal, blockIndex: event.blockIndex };
}

function claudeToolStep(event: ToolEvent, call: ToolEvent | null, records: EventRecord[]): ActivityStep {
  const missingCall = event.kind === "output" && call === null;
  const callPayload = call?.payload ?? (event.kind === "call" ? event.payload : undefined);
  const outputPayload = event.kind === "output" ? event.payload : null;
  return {
    key: event.key,
    kind: "tool",
    records,
    name: callPayload === undefined ? "Tool result" : (nonemptyString(callPayload.name) ?? "Tool call"),
    input: callPayload === undefined ? null : displayValue(callPayload.input),
    output: outputPayload === null ? null : claudeToolOutput(outputPayload.content),
    failed: outputPayload?.is_error === true,
    missingCall,
  };
}

function claudeToolOutput(value: unknown): string | null {
  if (
    Array.isArray(value) &&
    value.every((item) => object(item)?.type === "text" && typeof object(item)?.text === "string")
  ) {
    return value.map((item) => object(item)!.text as string).join("\n");
  }
  return displayValue(value);
}

function codexToolStep(event: ToolEvent, call: ToolEvent | null, records: EventRecord[]): ActivityStep {
  const missingCall = event.kind === "output" && call === null;
  const callPayload = call?.payload ?? event.payload;
  const outputPayload = event.kind === "output" ? event.payload : null;
  const callType = string(callPayload.type);
  const failed =
    callPayload.is_error === true ||
    callPayload.error != null ||
    outputPayload?.is_error === true ||
    outputPayload?.error != null;
  return {
    key: event.key,
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

function claudeThinkingEntries(record: EventRecord): Array<{ blockIndex: number; text: string }> {
  const content = claudeContent(record);
  if (content === null) return [];
  return content.flatMap((value, blockIndex) => {
    const block = object(value);
    if (block?.type !== "thinking") return [];
    const text = claudeThinkingText(block);
    return text === null ? [] : [{ blockIndex, text }];
  });
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

function uniqueRecords(records: readonly EventRecord[]): EventRecord[] {
  const byOrdinal = new Map<number, EventRecord>();
  for (const record of records) byOrdinal.set(record.ordinal, record);
  return [...byOrdinal.values()].sort((left, right) => left.ordinal - right.ordinal);
}

function sortRecords(records: readonly EventRecord[]): EventRecord[] {
  return uniqueRecords(records);
}

/** Counts semantic steps without presenting an unmatched result as a known tool call. */
export function summarizeActivity(steps: readonly ActivityStep[]): string {
  const calls = steps.filter((step) => step.kind === "tool" && !step.missingCall);
  const results = steps.filter((step) => step.kind === "tool" && step.missingCall);
  const reasoning = steps.filter((step) => step.kind === "reasoning");
  const agents = steps.filter((step) => step.kind === "subagent");
  const attachments = steps.filter((step) => step.kind === "attachment");
  const records = steps.filter((step) => step.kind === "record");
  const summary: string[] = [];

  if (calls.length > 0) summary.push(countLabel(calls.length, "tool call"));
  if (results.length > 0) summary.push(countLabel(results.length, "tool result"));
  if (reasoning.length > 0) summary.push(countLabel(reasoning.length, "reasoning step"));
  if (agents.length > 0) summary.push(countLabel(agents.length, "agent message"));
  if (attachments.length > 0) summary.push(countLabel(attachments.length, "attachment"));
  if (records.length > 0) summary.push(countLabel(records.length, "activity record"));
  return summary.join(", ");
}

function countLabel(count: number, singular: string): string {
  return count + " " + singular + (count === 1 ? "" : "s");
}
