// Per-tool-type rendering for haku-console's in-process `haku_routine` MCP server (see
// haku/console/tools/routine.py). `launch_routine` starts a new Haku claude-code-web run; its
// only argument is the optional per-run instruction text, which the operator wants to read
// verbatim before approving. Its validator comes from the exact FastMCP input schema advertised
// by tools/list, through the same generated catalog as the Gmail and Calendar previews.

import { Text } from "@mantine/core";
import { z } from "zod";

import { Field } from "../field.tsx";
import { mcpToolSchema } from "../mcp_tool_schema.ts";
import { definePreview, type ToolPreview } from "./entry.tsx";
import { clampBlock, type PreviewProps } from "./variant.tsx";

export const HAKU_ROUTINE_SERVER_ID = "haku_routine";

const zLaunchRoutineArgs = mcpToolSchema(HAKU_ROUTINE_SERVER_ID, "launch_routine");
type LaunchRoutineArgs = z.infer<typeof zLaunchRoutineArgs>;

function LaunchRoutinePreview({ args, variant }: PreviewProps<LaunchRoutineArgs>) {
  const text = args.text?.trim();
  const shown = text ? (variant === "compact" ? clampBlock(text, 3) : text) : null;
  return (
    <Field label="Instructions">
      {shown ? (
        <pre className="haku-shell-json">{shown}</pre>
      ) : (
        <Text size="sm" c="dimmed">
          (routine default)
        </Text>
      )}
    </Field>
  );
}

export const hakuRoutinePreviews = {
  launch_routine: definePreview(zLaunchRoutineArgs, LaunchRoutinePreview, () => ({ text: "Haku: Start a new run" })),
} satisfies Record<string, ToolPreview>;
