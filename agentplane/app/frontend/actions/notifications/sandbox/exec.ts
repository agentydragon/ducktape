import { z } from "zod";

import type { ActionRequestView } from "../../types";
import { sandboxExecNotes } from "../../schemas/sandbox/exec";
import { commandNotificationText } from "../command";
import type { ActionNotificationParts } from "../types";

const sandboxExecNotificationArguments = z.strictObject({
  name: z.string().min(1),
  script: z.string().min(1),
  timeout_seconds: z.number().int().positive(),
  max_output_bytes: z.number().int().nonnegative(),
  cwd: z.string().nullable().optional(),
});

export function sandboxExecNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = sandboxExecNotificationArguments.safeParse(request.arguments);
  if (!parsed.success) return null;

  return {
    actionTitle: `Run command in sandbox ${parsed.data.name}`,
    text: commandNotificationText(parsed.data.script, sandboxExecNotes(parsed.data)),
  };
}
