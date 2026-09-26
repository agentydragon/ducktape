// The `exec` tool of the SSH MCP server (x/ssh_mcp_server/server.py): the call as the operator decides
// it, the target and the exact command, and what came back, the exit code and the output.
import { Badge, Button, Code, Group, Stack, Text } from "@mantine/core";
import { type JSX, useState } from "react";
import { z } from "zod";

import { HighlightedCode } from "../syntax_highlight";
import { definePreview, type ArgumentsPreview, type PreviewProps } from "./entry";
import { defineResultPreview, type ResultPreview, type ResultPreviewProps } from "./result_entry";

// The tool's input schema. Strict, because the widget draws only these: a call carrying any other
// argument shows as its JSON, so nothing it would run with goes unseen.
const execArguments = z.strictObject({
  host: z.string().min(1),
  user: z.string().min(1),
  command: z.string().min(1),
  timeout_seconds: z.int().min(1).nullish(),
});

// The tool's `ExecResult`.
const execResult = z.strictObject({
  host: z.string(),
  user: z.string(),
  exit_code: z.int(),
  stdout: z.string(),
  stderr: z.string(),
  stdout_truncated: z.boolean(),
  stderr_truncated: z.boolean(),
});

// Past this many lines, an output stream shows its first lines and a button for the rest.
const COLLAPSED_LINES = 20;

// Wrapped, so a long output line reads where it sits instead of scrolling sideways, as the
// highlighted command does.
const WRAPPED = { whiteSpace: "pre-wrap", overflowWrap: "anywhere" } as const;

function ExecArguments({ args }: PreviewProps<z.infer<typeof execArguments>>): JSX.Element {
  return (
    <Stack gap={4}>
      <Text size="sm" fw={600} ff="monospace" style={{ overflowWrap: "anywhere" }}>
        {args.user}@{args.host}
      </Text>
      <HighlightedCode text={args.command} language="bash" />
      {args.timeout_seconds != null && (
        <Text size="xs" c="dimmed">
          Timeout {args.timeout_seconds} s
        </Text>
      )}
    </Stack>
  );
}

function ExecResult({ result }: ResultPreviewProps<z.infer<typeof execResult>>): JSX.Element {
  return (
    <Stack gap="xs">
      <Group gap="xs">
        <Badge color={result.exit_code === 0 ? "green" : "red"}>Exit {result.exit_code}</Badge>
        <Text size="xs" c="dimmed" ff="monospace" style={{ overflowWrap: "anywhere" }}>
          {result.user}@{result.host}
        </Text>
      </Group>
      <OutputStream name="stdout" text={result.stdout} truncated={result.stdout_truncated} />
      <OutputStream name="stderr" text={result.stderr} truncated={result.stderr_truncated} />
      {result.stdout === "" && result.stderr === "" && (
        <Text size="sm" c="dimmed">
          No output.
        </Text>
      )}
    </Stack>
  );
}

/** One output stream, as plain text, left out when empty. The server keeps only so many bytes of each,
 * and says when it dropped the rest. */
function OutputStream({
  name,
  text,
  truncated,
}: {
  name: string;
  text: string;
  truncated: boolean;
}): JSX.Element | null {
  const [expanded, setExpanded] = useState(false);
  if (text === "") return null;
  // A final newline ends the last line rather than starting an empty one.
  const lines = text.replace(/\n$/, "").split("\n");
  const long = lines.length > COLLAPSED_LINES;
  return (
    <div>
      <Text size="xs" c="dimmed" mb={4}>
        {name}
        {truncated && (
          <Text span size="xs" c="orange">
            {" "}
            · truncated by the server
          </Text>
        )}
      </Text>
      <Code block style={WRAPPED}>
        {(long && !expanded ? lines.slice(0, COLLAPSED_LINES) : lines).join("\n")}
      </Code>
      {long && (
        <Button
          variant="subtle"
          size="compact-xs"
          mt={4}
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? `Show the first ${COLLAPSED_LINES} lines` : `Show all ${lines.length} lines`}
        </Button>
      )}
    </div>
  );
}

export const execArgumentsPreview: ArgumentsPreview = definePreview(execArguments, ExecArguments);
export const execResultPreview: ResultPreview = defineResultPreview(execResult, ExecResult);
