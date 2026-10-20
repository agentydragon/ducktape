// The `exec` tool of the SSH MCP server (ssh_mcp_server/server.py): the call as the operator decides
// it, the target and the exact command, and what came back, the exit code and the output.
import { Badge, Code, Group, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import type { z } from "zod";

import type { ActionPresentation } from "../presentation";
import { CodeBlock } from "../../../code_block";
import { CommandCallView, CommandSummary, OutputBlock } from "../../../command_view";
import { definePreview, type ArgumentsPreview, type PreviewProps } from "../entry";
import { defineResultPreview, type ResultPreview, type ResultPreviewProps } from "../result_entry";
import { zSshExecArguments, zSshExecResult } from "../../schemas/ssh/exec";

type SshExecArguments = z.infer<typeof zSshExecArguments>;
type SshExecResult = z.infer<typeof zSshExecResult>;

function ExecArguments({ args }: PreviewProps<SshExecArguments>): JSX.Element {
  return (
    <CommandCallView
      target={`${args.user}@${args.host}`}
      command={args.command}
      notes={args.timeout_seconds == null ? [] : [`Timeout ${args.timeout_seconds} s`]}
      releaseWhenFar
      surface="framed"
    />
  );
}

function ExecLabel({ args }: PreviewProps<SshExecArguments>): JSX.Element {
  void args;
  return (
    <Text size="sm" fw={600}>
      Run SSH command
    </Text>
  );
}

function ExecCollapsed({ args }: PreviewProps<SshExecArguments>): JSX.Element {
  return (
    <CommandSummary
      text={args.command}
      lead={
        <>
          <Code>
            {args.user}@{args.host}
          </Code>
          {" · "}
          <Text span ff="monospace">
            {"$ "}
          </Text>
        </>
      }
    />
  );
}

function ExecResult({ result }: ResultPreviewProps<SshExecResult>): JSX.Element {
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
          releaseWhenFar
          surface="framed"
        />
      )}
      {result.stderr !== "" && (
        <OutputBlock
          name="stderr"
          note={result.stderr_truncated ? "truncated by the server" : undefined}
          text={result.stderr}
          releaseWhenFar
          surface="framed"
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

export const execArgumentsPreview: ArgumentsPreview = definePreview(zSshExecArguments, ExecArguments);
export const execLabel: ArgumentsPreview = definePreview(zSshExecArguments, ExecLabel);
export const execCollapsedPreview: ArgumentsPreview = definePreview(zSshExecArguments, ExecCollapsed);
export const execResultPreview: ResultPreview = defineResultPreview(zSshExecResult, ExecResult);

export const execPresentation: ActionPresentation = {
  outcome: (value) => {
    const parsed = zSshExecResult.safeParse(value);
    if (!parsed.success || parsed.data.exit_code === 0) return undefined;
    return { label: `Command exited ${parsed.data.exit_code}`, failed: true };
  },
  label: execLabel,
  pane: { collapsed: execCollapsedPreview, opened: execArgumentsPreview },
  details: { arguments: execArgumentsPreview, result: execResultPreview },
};
