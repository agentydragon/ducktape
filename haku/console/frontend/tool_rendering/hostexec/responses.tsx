// Result rendering for `hostexec_run` (see requests.tsx for the argument-side widget and the
// note on why this schema is hand-authored rather than generated). Mirrors `BaseExecResult`
// (mcp_infra/exec/models.py): a discriminated exit status plus stdout/stderr, each either the full
// text or a `TruncatedStream` when the process produced more than `max_bytes`.

import { Group, Stack } from "@mantine/core";
import { z } from "zod";

import { CodeBlock } from "../../code_block.tsx";
import { clampBlock, PreviewBadge, PreviewText, type PreviewVariant } from "../vocabulary.tsx";
import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry.tsx";

const zExitStatus = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("timed_out") }),
  z.object({ kind: z.literal("exited"), exit_code: z.number() }),
  z.object({ kind: z.literal("killed"), signal: z.number() }),
]);
const zExecStream = z.union([z.string(), z.object({ truncated_text: z.string(), total_bytes: z.number() })]);
const zHostexecRunResult = z.object({
  exit: zExitStatus,
  stdout: zExecStream,
  stderr: zExecStream,
  duration_ms: z.number(),
});

export type HostexecRunResult = z.infer<typeof zHostexecRunResult>;
type ExitStatus = z.infer<typeof zExitStatus>;
type ExecStream = z.infer<typeof zExecStream>;

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

function HostexecRunResultView({ result, variant }: ResultPreviewProps<HostexecRunResult>) {
  return (
    <Stack gap="xs">
      <Group gap={8}>
        <ExitBadge exit={result.exit} />
        <PreviewText size="xs" c="dimmed">
          {(result.duration_ms / 1000).toFixed(1)}s
        </PreviewText>
      </Group>
      <StreamBlock label="stdout" stream={result.stdout} variant={variant} />
      <StreamBlock label="stderr" stream={result.stderr} variant={variant} />
    </Stack>
  );
}

export const hostexecResultPreviews = {
  hostexec_run: defineResultPreview(zHostexecRunResult, HostexecRunResultView),
} satisfies Record<string, ToolResultPreview>;
