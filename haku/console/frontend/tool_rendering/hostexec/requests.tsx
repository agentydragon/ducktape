// Per-tool-type rendering for haku-console's in-process `hostexec` MCP server (see
// haku/console/tools/hostexec.py). `hostexec_run` runs `cmd` as a bash script (`bash -c cmd`) on
// an operator machine as a chosen POSIX user; every call is manually approved (never
// auto-approved), so showing the exact script unambiguously matters more than for narrower-scoped
// tools.
//
// hostexec isn't yet wired into the build-time in-process schema catalog (`export_mcp_tool_schemas.py`
// only builds gmail/google_calendar/routine for schema reflection today), so — like the remote
// kubectl-passthrough-mcp and grocy-sf servers — this schema is hand-authored against the real tool
// definition (`haku/console/tools/hostexec.py`, `mcp_infra/exec/models.py`) rather than generated
// from `mcp_tool_schema.ts`.

import { Stack } from "@mantine/core";
import { z } from "zod";

import { CodeBlock } from "../../code_block.tsx";
import { definePreview, type ToolPreview } from "../entry.tsx";
import { clampBlock, PreviewText, PreviewTitle, type PreviewProps } from "../vocabulary.tsx";

export const HOSTEXEC_SERVER_ID = "hostexec";

const zHostexecRunArgs = z.object({
  host: z.string(),
  // Mirrors RunAsUser (haku/hostexec/wire.py): `^[a-z_][a-z0-9_-]*$`, max 32 chars.
  run_as: z.string(),
  cmd: z.string().min(1),
  max_bytes: z.number().int().min(0).max(100_000),
  timeout_ms: z.number().int().gt(0).max(300_000),
  cwd: z.string().nullish(),
});

export type HostexecRunArgs = z.infer<typeof zHostexecRunArgs>;

// timeout_ms is capped at 300_000 (5 minutes), so plain seconds always reads naturally.
function formatTimeoutMs(ms: number): string {
  return ms % 1000 === 0 ? `${ms / 1000}s` : `${(ms / 1000).toFixed(1)}s`;
}

function formatMaxBytes(bytes: number): string {
  if (bytes === 0) return "0 B (no output captured)";
  return bytes % 1000 === 0 ? `${bytes / 1000} KB` : `${bytes} B`;
}

function HostexecTarget({ args }: { args: HostexecRunArgs }) {
  return (
    <PreviewTitle className="haku-shell-mono">
      {args.run_as}@{args.host}
      {args.cwd && (
        <PreviewText span c="dimmed" fw={400}>
          {" "}
          in {args.cwd}
        </PreviewText>
      )}
    </PreviewTitle>
  );
}

function HostexecRunPreview({ args, variant }: PreviewProps<HostexecRunArgs>) {
  if (variant === "compact") {
    return (
      <Stack gap={4}>
        <HostexecTarget args={args} />
        <CodeBlock language="shell" value={clampBlock(args.cmd, 3)} compact />
      </Stack>
    );
  }
  return (
    <Stack gap="xs">
      <HostexecTarget args={args} />
      <CodeBlock language="shell" value={args.cmd} lineNumbers />
      <PreviewText size="xs" c="dimmed">
        timeout {formatTimeoutMs(args.timeout_ms)} · max output {formatMaxBytes(args.max_bytes)}
      </PreviewText>
    </Stack>
  );
}

export const hostexecPreviews = {
  hostexec_run: definePreview(zHostexecRunArgs, HostexecRunPreview, (a) => ({
    text: `hostexec: Run on ${a.host} as ${a.run_as}`,
  })),
} satisfies Record<string, ToolPreview>;
