// Result rendering for hostexec's `bash` tool (see requests.tsx for the argument-side widget).
// Mirrors `BaseExecResult` (mcp_infra/exec/models.py): a discriminated exit status plus
// stdout/stderr, each either the full text or a `TruncatedStream` when the process produced more
// than `max_bytes`.

import { Group, Stack } from "@mantine/core";
import prettyMs from "pretty-ms";
import type { z } from "zod";

import { CodeBlock } from "../../code_block";
import { mcpToolResultSchema } from "../../mcp_tool_result_schema";
import { clampBlock, PreviewBadge, PreviewText, type PreviewVariant } from "../vocabulary";
import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry";
import { HOSTEXEC_SERVER_ID } from "../server_ids";

const zBashResult = mcpToolResultSchema(HOSTEXEC_SERVER_ID, "bash");

export type BashResult = z.infer<typeof zBashResult>;
type ExitStatus = BashResult["exit"];
type ExecStream = BashResult["stdout"];

function ExitBadge({ exit }: { exit: ExitStatus }) {
  switch (exit.kind) {
    case "exited":
      return (
        <PreviewBadge color={exit.exit_code === 0 ? "green" : "red"} variant="light">
          exit {exit.exit_code}
        </PreviewBadge>
      );
    case "killed":
      return (
        <PreviewBadge color="red" variant="light">
          killed (signal {exit.signal})
        </PreviewBadge>
      );
    case "timed_out":
      return (
        <PreviewBadge color="red" variant="light">
          timed out
        </PreviewBadge>
      );
  }
}

function streamText(stream: ExecStream): { text: string; truncated: boolean; totalBytes: number } {
  if (typeof stream === "string") return { text: stream, truncated: false, totalBytes: stream.length };
  return { text: stream.truncated_text, truncated: true, totalBytes: stream.total_bytes };
}

function StreamBlock({ label, stream, variant }: { label: string; stream: ExecStream; variant: PreviewVariant }) {
  const { text, truncated, totalBytes } = streamText(stream);
  if (!text) return null;
  const compact = variant === "compact";
  return (
    <Stack gap={2}>
      <PreviewText size="xs" c="dimmed">
        {label}
        {truncated && ` (truncated to ${text.length} of ${totalBytes} bytes)`}
      </PreviewText>
      <CodeBlock value={compact ? clampBlock(text, 4) : text} />
    </Stack>
  );
}

function BashResultView({ result, variant }: ResultPreviewProps<BashResult>) {
  return (
    <Stack gap="xs">
      <Group gap={8}>
        <ExitBadge exit={result.exit} />
        <PreviewText size="xs" c="dimmed">
          {prettyMs(result.duration_ms)}
        </PreviewText>
      </Group>
      <StreamBlock label="stdout" stream={result.stdout} variant={variant} />
      <StreamBlock label="stderr" stream={result.stderr} variant={variant} />
    </Stack>
  );
}

export const hostexecResultPreviews = {
  bash: defineResultPreview(zBashResult, BashResultView),
} satisfies Record<string, ToolResultPreview>;
