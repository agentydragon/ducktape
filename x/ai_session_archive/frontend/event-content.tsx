import { Code, Group, Stack, Text } from "@mantine/core";
import type { JSX } from "react";

import { Markdown } from "../../../util/frontend/markdown";
import { DisclosureBody } from "../../../util/frontend/disclosure";
import { RetainedDisclosure } from "../../../util/frontend/retained_disclosures";
import type { EventRecord } from "./schema";

export function recordLabel(record: EventRecord): string {
  const parsed = object(record.parsed);
  if (parsed === null) return record.kind || "Record";
  const payload = object(parsed.payload);
  if (parsed.type === "user" || parsed.type === "assistant") return parsed.type;
  if (parsed.type === "system") return typeof parsed.subtype === "string" ? `system · ${parsed.subtype}` : "system";
  if (parsed.type === "response_item" && payload !== null) {
    if (payload.type === "message" && typeof payload.role === "string") return payload.role;
    if (typeof payload.type === "string") return payload.type.replaceAll("_", " ");
  }
  if (parsed.type === "event_msg" && payload !== null && typeof payload.type === "string") {
    return `Codex · ${payload.type.replaceAll("_", " ")}`;
  }
  return record.kind || (typeof parsed.type === "string" ? parsed.type : "Record");
}

export function RecordContent({ record }: { record: EventRecord }): JSX.Element {
  if (record.parse_error != null) {
    return (
      <Stack gap="xs">
        <Text size="sm" c="orange">
          This JSONL line could not be parsed. Its original text is available below.
        </Text>
        <Text size="xs" c="dimmed">
          {record.parse_error}
        </Text>
      </Stack>
    );
  }

  const parsed = object(record.parsed);
  if (parsed === null)
    return (
      <Text size="sm" c="dimmed">
        Structured record; expand raw data to inspect it.
      </Text>
    );

  const content = claudeContent(parsed) ?? codexContent(parsed) ?? [];
  if (content.length > 0) {
    return (
      <Stack gap="xs">
        {content.map((part, index) => (
          <ContentPart key={`${part.kind}-${index}`} id={`part:${record.ordinal}:${index}`} part={part} />
        ))}
      </Stack>
    );
  }

  const payload = object(parsed.payload);
  const summary = payload === null ? null : (text(payload.message) ?? text(payload.summary) ?? text(payload.output));
  return summary === null ? (
    <Text size="sm" c="dimmed">
      Structured record; expand raw data to inspect it.
    </Text>
  ) : (
    <Markdown source={summary} />
  );
}

type Part = { kind: string; text?: string; value?: unknown };

function ContentPart({ part, id }: { part: Part; id: string }): JSX.Element {
  if (part.kind === "text" && part.text !== undefined) return <Markdown source={part.text} />;
  if (part.kind === "thinking" && part.text !== undefined) {
    return (
      <RetainedDisclosure id={id} summary="Thinking">
        <DisclosureBody>
          <Markdown source={part.text} />
        </DisclosureBody>
      </RetainedDisclosure>
    );
  }
  return (
    <Stack gap={4}>
      <Group gap="xs">
        <Text size="sm" fw={600}>
          {part.kind}
        </Text>
      </Group>
      {part.value !== undefined && (
        <Code block style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {formatValue(part.value)}
        </Code>
      )}
    </Stack>
  );
}

function claudeContent(parsed: Record<string, unknown>): Part[] | null {
  const message = object(parsed.message);
  if (message === null) return null;
  return parts(message.content, (part) => {
    if (part.type === "text" && typeof part.text === "string") return { kind: "text", text: part.text };
    if (part.type === "thinking" && typeof part.thinking === "string") {
      return { kind: "thinking", text: part.thinking };
    }
    if (part.type === "tool_use") {
      return { kind: `Tool call · ${text(part.name) ?? "unknown"}`, value: part.input ?? part };
    }
    if (part.type === "tool_result") {
      return { kind: "Tool result", value: part.content ?? part };
    }
    return { kind: text(part.type) ?? "Content block", value: part };
  });
}

function codexContent(parsed: Record<string, unknown>): Part[] | null {
  const payload = object(parsed.payload);
  if (payload === null) return null;
  if (payload.type === "message") {
    return parts(payload.content, (part) => {
      if (["input_text", "output_text", "text"].includes(text(part.type) ?? "") && typeof part.text === "string") {
        return { kind: "text", text: part.text };
      }
      return { kind: text(part.type) ?? "Content block", value: part };
    });
  }
  if (payload.type === "reasoning") {
    return parts(payload.summary, (part) => ({
      kind: "thinking",
      text: text(part.text) ?? "Reasoning details unavailable",
    }));
  }
  if (payload.type === "function_call" || payload.type === "custom_tool_call") {
    const name = text(payload.name) ?? text(payload.type)?.replaceAll("_", " ") ?? "Tool call";
    return [{ kind: `Tool call · ${name}`, value: payload.arguments ?? payload.input ?? payload }];
  }
  if (payload.type === "function_call_output" || payload.type === "custom_tool_call_output") {
    return [{ kind: "Tool result", value: payload.output ?? payload }];
  }
  return null;
}

function parts(value: unknown, convert: (part: Record<string, unknown>) => Part): Part[] {
  if (typeof value === "string") return value.length === 0 ? [] : [{ kind: "text", text: value }];
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const part = object(item);
    return part === null ? [] : [convert(part)];
  });
}

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

export function rawRecordText(rawBase64: string): string {
  try {
    const binary = atob(rawBase64);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    return new TextDecoder("utf-8").decode(bytes);
  } catch {
    return "The raw line could not be decoded as UTF-8. Use the base64 disclosure for its exact bytes.";
  }
}
