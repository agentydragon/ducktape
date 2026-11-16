import { object } from "./json-values";
import type { ObjectValue } from "./json-values";
import type { EventRecord } from "./schema";

export type AgentMessageContent = { kind: "text"; text: string } | { kind: "encrypted"; text: string | null };

export function responsePayload(record: EventRecord): ObjectValue | null {
  const parsed = object(record.parsed);
  return parsed?.type === "response_item" ? object(parsed.payload) : null;
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
