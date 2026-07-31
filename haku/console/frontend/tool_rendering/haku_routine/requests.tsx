// Per-tool-type rendering for haku-console's in-process `haku_routine` MCP server (see
// haku/console/tools/routine.py). `launch_routine` starts a new Haku claude-code-web run; its
// only argument is the optional per-run instruction text, which the operator wants to read
// verbatim before approving. Its validator comes from the exact FastMCP input schema advertised
// by tools/list, through the same generated catalog as the Gmail and Calendar previews.

import { z } from "zod";

import { CodeBlock } from "../../code_block";
import { Field } from "../../field";
import { mcpToolSchema } from "../../mcp_tool_schema";
import { definePreview, type ToolPreview } from "../entry";
import { clampBlock, PreviewText, type PreviewProps } from "../vocabulary";
import { HAKU_ROUTINE_SERVER_ID } from "../server_ids";

const zLaunchRoutineArgs = mcpToolSchema(HAKU_ROUTINE_SERVER_ID, "launch_routine");
type LaunchRoutineArgs = z.infer<typeof zLaunchRoutineArgs>;

function LaunchRoutinePreview({ args, variant }: PreviewProps<LaunchRoutineArgs>) {
  const text = args.text?.trim();
  const shown = text ? (variant === "compact" ? clampBlock(text, 3) : text) : null;
  return (
    <Field label="Instructions">
      {shown ? <CodeBlock value={shown} /> : <PreviewText c="dimmed">(routine default)</PreviewText>}
    </Field>
  );
}

export const hakuRoutinePreviews = {
  launch_routine: definePreview(zLaunchRoutineArgs, LaunchRoutinePreview),
} satisfies Record<string, ToolPreview>;
