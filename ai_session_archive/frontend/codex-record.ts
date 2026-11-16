import { nonemptyString, object, string } from "./json-values";
import type { ObjectValue } from "./json-values";
import type { EventRecord } from "./schema";
import type { ActivityStep, AgentMessageContent, ToolEvent } from "./transcript-row-types";

const TOOL_CALL_TYPES = new Set(["function_call", "custom_tool_call"]);
const TOOL_OUTPUT_TYPES = new Set(["function_call_output", "custom_tool_call_output"]);

type CodexToolEventFields = Omit<ToolEvent, "namespace">;

type CodexToolEvent = CodexToolEventFields & { namespace: "codex" };

export function responsePayload(record: EventRecord): ObjectValue | null {
  const parsed = object(record.parsed);
  return parsed?.type === "response_item" ? object(parsed.payload) : null;
}

export function codexToolEvent(record: EventRecord): CodexToolEvent | null {
  if (record.parse_error != null) return null;
  const payload = responsePayload(record);
  const type = string(payload?.type);
  if (payload === null || type === null) return null;
  if (TOOL_CALL_TYPES.has(type)) {
    return {
      key: String(record.ordinal),
      namespace: "codex",
      kind: "call",
      payload,
      callId: nonemptyString(payload.call_id),
      record,
      blockIndex: 0,
    };
  }
  if (TOOL_OUTPUT_TYPES.has(type)) {
    return {
      key: String(record.ordinal),
      namespace: "codex",
      kind: "output",
      payload,
      callId: nonemptyString(payload.call_id),
      record,
      blockIndex: 0,
    };
  }
  return null;
}

export function codexToolStep(
  event: CodexToolEventFields,
  call: CodexToolEventFields | null,
  records: EventRecord[],
  displayValue: (value: unknown) => string | null
): Extract<ActivityStep, { kind: "tool" }> {
  const missingCall = event.kind === "output" && call === null;
  const callPayload = call?.payload ?? event.payload;
  const outputPayload = event.kind === "output" ? event.payload : null;
  const callType = string(callPayload.type);
  return {
    key: event.key,
    kind: "tool" as const,
    records,
    name: missingCall
      ? "Tool result"
      : (nonemptyString(callPayload.name) ?? (callType === "custom_tool_call" ? "Custom tool" : "Tool call")),
    input: missingCall ? null : codexToolInput(callPayload, displayValue),
    output: outputPayload === null ? null : codexToolOutput(outputPayload.output, displayValue),
    // Codex's typed tool-output record has no failure flag. Keep error-like extra fields in Raw JSONL.
    failed: false,
    missingCall,
  };
}

function codexToolInput(payload: ObjectValue, displayValue: (value: unknown) => string | null): string | null {
  if (payload.type === "function_call") return displayValue(payload.arguments);
  if (payload.type === "custom_tool_call") return displayValue(payload.input);
  return null;
}

function codexToolOutput(value: unknown, displayValue: (value: unknown) => string | null): string | null {
  if (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((item) => object(item)?.type === "input_text" && typeof object(item)?.text === "string")
  ) {
    return value.map((item) => object(item)!.text as string).join("\n");
  }
  return displayValue(value);
}

export function reasoningText(payload: ObjectValue): string | null {
  if (!Array.isArray(payload.summary)) return null;
  const chunks = payload.summary.flatMap((value) => {
    const part = object(value);
    if (part?.type !== "summary_text" || typeof part.text !== "string" || part.text.trim().length === 0) return [];
    return [part.text];
  });
  return chunks.length === 0 ? null : chunks.join("\n");
}

export function agentMessageContent(payload: ObjectValue): AgentMessageContent | null {
  if (!Array.isArray(payload.content)) return null;
  const chunks: string[] = [];
  let encrypted = false;
  // Observed Codex rollout shape: input_text blocks, optionally followed by encrypted_content.
  for (const value of payload.content) {
    const block = object(value);
    if (block?.type === "input_text" && typeof block.text === "string") {
      const body = block.text.replace(AGENT_ROUTING_PREAMBLE, "");
      if (body.trim().length > 0) chunks.push(body);
    } else if (block?.type === "encrypted_content" && typeof block.encrypted_content === "string") {
      encrypted = true;
    } else {
      return null;
    }
  }
  const text = chunks.length > 0 ? chunks.join("\n") : null;
  if (encrypted) return { kind: "encrypted", text };
  return text === null ? null : { kind: "text", text };
}

// Codex archives can store the agent-routing header as text before encrypted message content.
const AGENT_ROUTING_PREAMBLE =
  /^Message Type: MESSAGE\r?\nTask name: \/root(?:\/[A-Za-z0-9_-]+)*\r?\nSender: \/root(?:\/[A-Za-z0-9_-]+)*\r?\nPayload:\r?\n/;
