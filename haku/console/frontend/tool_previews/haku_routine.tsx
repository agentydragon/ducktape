// Per-tool-type rendering for haku-console's in-process `haku_routine` MCP server (see
// haku/console/tools/routine.py). `launch_routine` starts a new Haku claude-code-web run; its
// only argument is the optional per-run instruction text, which the operator wants to read
// verbatim before approving. Hand-authored zod (there's no backend Pydantic arg model wired to
// :schema_zod for one trivial optional string) — same caveat as kubectl.tsx.

import { Stack, Text } from "@mantine/core";
import { z } from "zod";

import { Field } from "../field.tsx";
import { definePreview, type ToolPreview } from "./entry.tsx";
import { clampBlock, type PreviewProps } from "./variant.tsx";

export const HAKU_ROUTINE_SERVER_ID = "haku_routine";

const zLaunchRoutineArgs = z.object({ text: z.string().nullish() });
type LaunchRoutineArgs = z.infer<typeof zLaunchRoutineArgs>;

function LaunchRoutinePreview({ args, variant }: PreviewProps<LaunchRoutineArgs>) {
  const text = args.text?.trim();
  const shown = text ? (variant === "compact" ? clampBlock(text, 3) : text) : null;
  return (
    <Stack gap="xs">
      <Field label="Action">Start a new Haku run</Field>
      <Field label="Instructions">
        {shown ? (
          <pre className="haku-shell-json">{shown}</pre>
        ) : (
          <Text size="sm" c="dimmed">
            (routine default)
          </Text>
        )}
      </Field>
    </Stack>
  );
}

export const hakuRoutinePreviews = {
  launch_routine: definePreview(zLaunchRoutineArgs, LaunchRoutinePreview),
} satisfies Record<string, ToolPreview>;
