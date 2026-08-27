import { Badge, Code, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
import type { ToolCallEntry } from "../client";

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

const SUMMARY_CHARACTERS = 110;

function snippet(value: string): string {
  const line = value.trim().split("\n")[0];
  return line.length <= SUMMARY_CHARACTERS ? line : `${line.slice(0, SUMMARY_CHARACTERS - 1)}…`;
}

/** A code-valued fragment of a folded line: a command, a path, a pattern. */
function mono(value: unknown): ReactNode | null {
  if (typeof value !== "string" || !value.trim()) return null;
  return <span className="haku-chat-tool-call-snippet-mono">{snippet(value)}</span>;
}

/** A prose-valued fragment of a folded line: a description, a query. */
function prose(value: unknown): ReactNode | null {
  if (typeof value !== "string" || !value.trim()) return null;
  return <>{snippet(value)}</>;
}

/** Per-tool folded lines, the transcript counterpart of `tool_rendering/`'s per-server widgets.
 *
 * `tool_rendering/` keys on the console's MCP servers; a transcript's calls are the CLI harness's
 * own tools, so they get their own registry. Each renderer says how *its* arguments identify the
 * call — Bash by its description (falling back to the command itself), a file tool by its path —
 * rather than one heuristic guessing across all of them. A tool without an entry falls back to the
 * first meaningful line of its raw arguments.
 */
function registeredSummary(toolName: string, args: Record<string, unknown>): ReactNode | null {
  // A switch, not a callable looked up by name: the tool name is wire data, and dynamic dispatch
  // on it — object keys and Map.get alike — is what CodeQL's unvalidated-dynamic-method-call
  // query rejects.
  switch (toolName) {
    case "Bash":
      return prose(args.description) ?? mono(args.command);
    case "BashOutput":
      return mono(args.bash_id);
    case "Write":
    case "Edit":
    case "MultiEdit":
    case "Read":
      return mono(args.file_path);
    case "NotebookEdit":
      return mono(args.notebook_path);
    case "Grep":
    case "Glob":
      return mono(args.pattern);
    case "WebFetch":
      return mono(args.url);
    case "WebSearch":
      return prose(args.query);
    case "Task":
      return prose(args.description);
    default:
      return null;
  }
}

function toolCallSummary(call: ToolCallEntry): ReactNode {
  const args = call.arguments;
  if (args !== null && typeof args === "object" && !Array.isArray(args)) {
    const rendered = registeredSummary(call.tool_name, args as Record<string, unknown>);
    if (rendered !== null) return rendered;
  }
  return mono(previewText(toolPayloadText(args)));
}

/** One call, whole: what was asked, what it printed, and what it produced that no string carries.
 *
 * The ask and the answer are one entry — `outcome` still null is what says no answer has arrived,
 * and `status` whether one ever will: an `open` call is running, a `failed` one was cut off by its
 * session dying.
 *
 * **Folded to one line by default.** In a transcript the calls are the agent's working, not its
 * answer, and an open card per call buried the prose between them. The folded line carries the
 * name, the argument that identifies the call, and its state; everything else — full arguments,
 * output, structured result — is behind the fold.
 */
export function ToolCallView({ call }: { call: ToolCallEntry }) {
  return (
    <details className="haku-chat-tool-call">
      <summary className="haku-chat-tool-call-summary">
        <span className="haku-chat-tool-call-name">{call.tool_name}</span>
        <span className="haku-chat-tool-call-snippet">{toolCallSummary(call)}</span>
        {(call.outcome === "failed" || call.status === "failed") && (
          <Badge variant="light" color="red">
            failed
          </Badge>
        )}
        {call.status === "open" && (
          <Badge variant="light" color="blue">
            running
          </Badge>
        )}
      </summary>
      <Stack gap="xs" className="haku-chat-tool-call-body">
        <ToolPayload label="Arguments" value={call.arguments} emptyLabel="No arguments." emptyWhenNoKeys />
        {call.outcome !== null ? (
          <>
            <ToolPayload
              label="Output"
              value={call.content}
              emptyLabel={call.outcome === "failed" ? "No error details captured." : "Empty result."}
            />
            {call.structured != null && <ToolPayload label="Structured" value={call.structured} emptyLabel="" />}
          </>
        ) : (
          <Text c="dimmed" size="xs">
            {call.status === "failed" ? "The session ended before this call was answered." : "No result yet."}
          </Text>
        )}
      </Stack>
    </details>
  );
}
