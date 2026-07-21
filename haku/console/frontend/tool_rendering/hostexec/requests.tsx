// Per-tool-type rendering for haku-console's in-process `hostexec` MCP server (see
// haku/console/tools/hostexec.py). Its one tool, `bash`, runs `cmd` as a bash script
// (`bash -c cmd`) on an operator machine as a chosen POSIX user; every call is manually approved
// (never auto-approved), so showing the exact script unambiguously matters more than for
// narrower-scoped tools.

import { Stack } from "@mantine/core";
import prettyBytes from "pretty-bytes";
import prettyMs from "pretty-ms";
import type { z } from "zod";

import { CodeBlock } from "../../code_block.tsx";
import { mcpToolSchema } from "../../mcp_tool_schema.ts";
import { definePreview, type ToolPreview } from "../entry.tsx";
import { clampBlock, PreviewText, PreviewTitle, type PreviewProps } from "../vocabulary.tsx";

export const HOSTEXEC_SERVER_ID = "hostexec";

const zBashArgs = mcpToolSchema(HOSTEXEC_SERVER_ID, "bash");

export type BashArgs = z.infer<typeof zBashArgs>;

function formatMaxBytes(bytes: number): string {
  return bytes === 0 ? "0 B (no output captured)" : prettyBytes(bytes);
}

function BashTarget({ args }: { args: BashArgs }) {
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

function BashPreview({ args, variant }: PreviewProps<BashArgs>) {
  if (variant === "compact") {
    return (
      <Stack gap={4}>
        <BashTarget args={args} />
        <CodeBlock language="shell" value={clampBlock(args.cmd, 3)} compact />
      </Stack>
    );
  }
  return (
    <Stack gap="xs">
      <BashTarget args={args} />
      <CodeBlock language="shell" value={args.cmd} lineNumbers />
      <PreviewText size="xs" c="dimmed">
        timeout {prettyMs(args.timeout_ms)} · max output {formatMaxBytes(args.max_bytes)}
      </PreviewText>
    </Stack>
  );
}

export const hostexecPreviews = {
  bash: definePreview(zBashArgs, BashPreview, (a) => ({
    text: `hostexec: Run on ${a.host} as ${a.run_as}`,
  })),
} satisfies Record<string, ToolPreview>;
