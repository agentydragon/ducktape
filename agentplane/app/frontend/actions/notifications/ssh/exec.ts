import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import { actionArguments } from "../../presentation";
import { sshExecNotes, zSshExecArguments } from "../../schemas/ssh/exec";
import { commandNotificationText } from "../command";
import type { ActionNotificationParts } from "../types";

export function sshExecNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = zSshExecArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;
  const { command, host, user } = parsed.data;
  return {
    actionTitle: `${user === "root" ? "⚠ " : ""}Run command on ${user}@${host}`,
    text: commandNotificationText(command, sshExecNotes(parsed.data)),
  };
}
