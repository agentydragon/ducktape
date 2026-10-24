import type { ActionRequestView } from "../../types";
import { sshExecNotes, zSshExecArguments } from "../../schemas/ssh/exec";
import { commandNotificationText } from "../command";
import type { ActionNotificationParts } from "../types";

export function sshExecNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = zSshExecArguments.safeParse(request.arguments);
  if (!parsed.success) return null;
  const { command, host, user } = parsed.data;
  return {
    actionTitle: `${user === "root" ? "⚠ " : ""}Run command on ${user}@${host}`,
    text: commandNotificationText(command, sshExecNotes(parsed.data)),
  };
}
