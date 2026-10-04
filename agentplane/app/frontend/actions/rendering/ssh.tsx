// The `exec` tool of the SSH MCP server (x/ssh_mcp_server/server.py): the call as the operator decides
// it, the target and the exact command, and what came back, the exit code and the output.
import { Badge, Group, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { CodeBlock } from "../../code_block";
import { CommandCallView, OutputBlock } from "../../command_view";
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

function ExecArguments({ args }: PreviewProps<z.infer<typeof execArguments>>): JSX.Element {
  return (
    <CommandCallView
      target={`${args.user}@${args.host}`}
      command={args.command}
      notes={args.timeout_seconds == null ? [] : [`Timeout ${args.timeout_seconds} s`]}
    />
  );
}

function ExecResult({ result }: ResultPreviewProps<z.infer<typeof execResult>>): JSX.Element {
  return (
    <Stack gap="xs">
      <Group gap="xs">
        <Badge color={result.exit_code === 0 ? "green" : "red"}>Exit {result.exit_code}</Badge>
        <CodeBlock text={`${result.user}@${result.host}`} presentation="muted" />
      </Group>
      {/* The server keeps only so many bytes of each stream, and says when it dropped the rest. */}
      {result.stdout !== "" && (
        <OutputBlock
          name="stdout"
          note={result.stdout_truncated ? "truncated by the server" : undefined}
          text={result.stdout}
        />
      )}
      {result.stderr !== "" && (
        <OutputBlock
          name="stderr"
          note={result.stderr_truncated ? "truncated by the server" : undefined}
          text={result.stderr}
        />
      )}
      {result.stdout === "" && result.stderr === "" && (
        <Text size="sm" c="dimmed">
          No output.
        </Text>
      )}
    </Stack>
  );
}

export const execArgumentsPreview: ArgumentsPreview = definePreview(execArguments, ExecArguments);
export const execResultPreview: ResultPreview = defineResultPreview(execResult, ExecResult);
