import { nonemptyString, object, string } from "./json-values";
import type { ObjectValue } from "./json-values";
import type { EventRecord } from "./schema";
import type { ActivityStep, RecordPresentation, StepCandidate, ToolEvent, ToolPairs } from "./transcript-row-types";

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

export function claudeRecordPresentation(record: EventRecord): RecordPresentation | null {
  if (record.parse_error != null) return null;
  const parsed = object(record.parsed);
  if (parsed?.type !== "user" && parsed?.type !== "assistant") {
    return parsed !== null && CLAUDE_METADATA.has(String(parsed.type)) ? "metadata" : null;
  }
  const content = object(parsed.message)?.content;
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

export function claudeToolEvents(record: EventRecord): ToolEvent[] {
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
          namespace: "claude",
          kind: "call",
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
          namespace: "claude",
          kind: "output",
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

export function claudeThinkingEntries(record: EventRecord): Array<{ blockIndex: number; text: string }> {
  const content = claudeContent(record);
  if (content === null) return [];
  return content.flatMap((value, blockIndex) => {
    const block = object(value);
    if (block?.type !== "thinking") return [];
    const text = claudeThinkingText(block);
    return text === null ? [] : [{ blockIndex, text }];
  });
}

export function claudeActivitySteps(
  record: EventRecord,
  pairs: ToolPairs,
  sourceOrdinals: ReadonlySet<number>,
  makeToolCandidate: (event: ToolEvent, call: ToolEvent | null) => StepCandidate
): StepCandidate[] | null {
  const content = claudeContent(record);
  if (content === null) return null;
  const candidates: StepCandidate[] = [];
  const eventsByIndex = new Map(claudeToolEvents(record).map((event) => [event.blockIndex, event]));
  content.forEach((value, blockIndex) => {
    const block = object(value);
    if (block === null) return;
    const event = eventsByIndex.get(blockIndex);
    if (event !== undefined) {
      if (event.kind === "call") {
        if (!pairs.outputByCallKey.has(event.key)) candidates.push(makeToolCandidate(event, null));
        return;
      }
      const call = pairs.callByOutputKey.get(event.key);
      candidates.push(
        makeToolCandidate(event, call !== undefined && sourceOrdinals.has(call.record.ordinal) ? call : null)
      );
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
  return candidates;
}

export function claudeToolStep(
  event: ToolEvent,
  call: ToolEvent | null,
  records: EventRecord[],
  displayValue: (value: unknown) => string | null
): ActivityStep {
  const missingCall = event.kind === "output" && call === null;
  const callPayload = call?.payload ?? (event.kind === "call" ? event.payload : undefined);
  const outputPayload = event.kind === "output" ? event.payload : null;
  return {
    key: event.key,
    kind: "tool",
    records,
    name: callPayload === undefined ? "Tool result" : (nonemptyString(callPayload.name) ?? "Tool call"),
    input: callPayload === undefined ? null : displayValue(callPayload.input),
    output: outputPayload === null ? null : claudeToolOutput(outputPayload.content, displayValue),
    failed: outputPayload?.is_error === true,
    missingCall,
  };
}

function claudeToolOutput(value: unknown, displayValue: (value: unknown) => string | null): string | null {
  if (
    Array.isArray(value) &&
    value.every((item) => object(item)?.type === "text" && typeof object(item)?.text === "string")
  ) {
    return value.map((item) => object(item)!.text as string).join("\n");
  }
  return displayValue(value);
}
