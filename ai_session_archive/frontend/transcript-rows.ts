import { agentMessageContent, codexToolEvent, codexToolStep, reasoningText, responsePayload } from "./codex-record";
import { nonemptyString, object, string } from "./json-values";
import type { EventRecord } from "./schema";
import {
  claudeActivitySteps,
  claudeRecordPresentation,
  claudeThinkingEntries,
  claudeToolEvents,
  claudeToolStep as buildClaudeToolStep,
} from "./claude-transcript";
import type {
  ActivityStep,
  RecordPresentation,
  StepCandidate,
  ToolEvent,
  ToolPairs,
  TranscriptRow,
} from "./transcript-row-types";

type MutableTranscriptRow =
  | { kind: "record"; records: EventRecord[] }
  | { kind: "metadata"; records: EventRecord[] }
  | { kind: "activity"; records: EventRecord[]; stepRecords: EventRecord[] };

function toolEvents(record: EventRecord): ToolEvent[] {
  const parsed = object(record.parsed);
  if (parsed?.type === "user" || parsed?.type === "assistant") {
    return claudeToolEvents(record);
  }

  const event = codexToolEvent(record);
  return event === null ? [] : [event];
}

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
  const claudePresentation = claudeRecordPresentation(record);
  if (claudePresentation !== null) return claudePresentation;
  if (parsed.type === "attachment" && object(parsed.attachment) !== null) return "activity";

  const payload = object(parsed.payload);
  if (parsed.type === "response_item" && payload) {
    const type = string(payload.type);
    if (codexToolEvent(record) !== null) return "activity";
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
  if (CODEX_METADATA.has(String(parsed.type))) return "metadata";
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
    const sorted = uniqueRecords(row.records);
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

    const claudeCandidates = claudeActivitySteps(record, pairs, sourceOrdinals, toolCandidate);
    if (claudeCandidates !== null) {
      candidates.push(...claudeCandidates);
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
      const content = agentMessageContent(payload);
      candidates.push({
        ordinal: record.ordinal,
        blockIndex: 0,
        step:
          content === null
            ? { key: String(record.ordinal), kind: "record", records: [record] }
            : {
                key: String(record.ordinal),
                kind: "subagent",
                records: [record],
                author: nonemptyString(payload.author) ?? "Agent",
                recipient: nonemptyString(payload.recipient),
                content,
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
    event.namespace === "claude"
      ? buildClaudeToolStep(event, call, records, displayValue)
      : codexToolStep(event, call, records, displayValue);
  return { step, ordinal: event.record.ordinal, blockIndex: event.blockIndex };
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

function uniqueRecords(records: readonly EventRecord[]): EventRecord[] {
  const byOrdinal = new Map<number, EventRecord>();
  for (const record of records) byOrdinal.set(record.ordinal, record);
  return [...byOrdinal.values()].sort((left, right) => left.ordinal - right.ordinal);
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
