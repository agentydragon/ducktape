import { Badge, Code, Group, Paper, Stack, Text } from "@mantine/core";
import type { ConversationItem } from "../client";

import { CodeBlock } from "../code_block";

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

function ToolPayload({
  label,
  value,
  emptyLabel,
  emptyWhenNoKeys = false,
}: {
  label: string;
  value: unknown;
  emptyLabel: string;
  /** An argument object with no keys is a call with no arguments; an empty result is not. */
  emptyWhenNoKeys?: boolean;
}) {
  if (value == null || (emptyWhenNoKeys && isEmptyObject(value))) {
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

/** One call, whole: what was asked, what it printed, and what it produced that no string carries.
 *
 * A call is an item, so its ask and its answer are the same row — `status` is what says whether the
 * answer has arrived, rather than a nested result being present or absent.
 */
export function ToolCallView({ item }: { item: ConversationItem }) {
  return (
    <Paper withBorder p="sm" radius="sm" className="haku-claude-tool-use">
      <Group gap="xs" mb="xs">
        <Badge variant="light" color="gray">
          Tool
        </Badge>
        <Code style={{ overflowWrap: "anywhere" }}>{item.tool_name}</Code>
        {item.outcome === "failed" && (
          <Badge variant="light" color="red">
            failed
          </Badge>
        )}
      </Group>
      <Stack gap="xs">
        <ToolPayload label="Arguments" value={item.arguments} emptyLabel="No arguments." emptyWhenNoKeys />
        {item.status === "complete" ? (
          <>
            <ToolPayload
              label="Output"
              value={item.text}
              emptyLabel={item.outcome === "failed" ? "No error details captured." : "Empty result."}
            />
            {item.structured != null && <ToolPayload label="Structured" value={item.structured} emptyLabel="" />}
          </>
        ) : (
          <Text c="dimmed" size="xs">
            No result yet.
          </Text>
        )}
      </Stack>
    </Paper>
  );
}
