import { Badge, Code, Group, Paper, Stack, Text } from "@mantine/core";
import type { ClaudeChatMessage } from "../client";

import { CodeBlock } from "../code_block";

type ClaudeToolUse = ClaudeChatMessage["tool_uses"][number];

const COLLAPSE_AFTER_CHARACTERS = 600;
const COLLAPSE_AFTER_LINES = 10;
const PREVIEW_CHARACTERS = 140;

/** Keep the wire's human-readable strings readable, while making structured blocks inspectable JSON. */
export function toolPayloadText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value) as unknown;
      if (parsed !== null && typeof parsed === "object") return JSON.stringify(parsed, null, 2);
    } catch {
      // Plain-text tool output is the common case; leave it as-is.
    }
    return value;
  }
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return String(value);
  }
}

export function shouldCollapseToolPayload(value: unknown): boolean {
  const text = toolPayloadText(value);
  return text.length > COLLAPSE_AFTER_CHARACTERS || text.split("\n").length > COLLAPSE_AFTER_LINES;
}

function isEmptyObject(value: unknown): boolean {
  return value !== null && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 0;
}

function previewText(text: string): string {
  const firstLine =
    text
      .split("\n")
      .find((line) => {
        const trimmed = line.trim();
        return trimmed && !["{", "[", "}", "]", "},", "],"].includes(trimmed);
      })
      ?.trim() ?? "";
  if (firstLine.length <= PREVIEW_CHARACTERS) return firstLine;
  return `${firstLine.slice(0, PREVIEW_CHARACTERS - 1)}…`;
}

function payloadSize(text: string): string {
  if (text.length < 1000) return `${text.length} chars`;
  return `${(text.length / 1000).toFixed(1)}k chars`;
}

function ToolPayload({ label, value, emptyLabel }: { label: string; value: unknown; emptyLabel: string }) {
  if (value == null || (label === "Input" && isEmptyObject(value))) {
    return (
      <Text c="dimmed" size="xs">
        {emptyLabel}
      </Text>
    );
  }

  const text = toolPayloadText(value);
  if (!text.trim()) {
    return (
      <Text c="dimmed" size="xs">
        {emptyLabel}
      </Text>
    );
  }

  const collapsed = shouldCollapseToolPayload(value);
  const language =
    typeof value === "object" && value !== null
      ? "json"
      : typeof value === "string" && toolPayloadText(value) !== value
        ? "json"
        : undefined;
  if (collapsed) {
    return (
      <details className="haku-shell-disclosure haku-claude-tool-payload">
        <summary>
          {label} · {payloadSize(text)} · {previewText(text)}
        </summary>
        <CodeBlock language={language} value={text} />
      </details>
    );
  }

  if (language) {
    return (
      <div className="haku-claude-tool-payload">
        <Text c="dimmed" size="xs" mb={4}>
          {label}
        </Text>
        <CodeBlock language="json" value={text} />
      </div>
    );
  }

  return (
    <div className="haku-claude-tool-payload">
      <Text c="dimmed" size="xs" mb={4}>
        {label}
      </Text>
      <Code block style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
        {text}
      </Code>
    </div>
  );
}

export function ClaudeToolUseView({ toolUse }: { toolUse: ClaudeToolUse }) {
  return (
    <Paper withBorder p="sm" radius="sm" className="haku-claude-tool-use">
      <Group gap="xs" mb="xs">
        <Badge variant="light" color="gray">
          Tool
        </Badge>
        <Code style={{ overflowWrap: "anywhere" }}>{toolUse.name}</Code>
        {toolUse.result?.is_error && (
          <Badge variant="light" color="red">
            failed
          </Badge>
        )}
      </Group>
      <Stack gap="xs">
        <ToolPayload label="Input" value={toolUse.input} emptyLabel="No arguments." />
        {toolUse.result ? (
          <ToolPayload
            label="Result"
            value={toolUse.result.content}
            emptyLabel={toolUse.result.is_error ? "No error details captured." : "Empty result."}
          />
        ) : (
          <Text c="dimmed" size="xs">
            No result yet.
          </Text>
        )}
      </Stack>
    </Paper>
  );
}
