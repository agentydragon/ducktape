import type { ActionRequestView } from "../../types";
import { zSshExecArguments } from "../../schemas/ssh/exec";
import { actionNotificationTitle } from "../../presentation_catalog";
import type { ActionNotificationContent } from "../types";

export function sshExecNotification(request: ActionRequestView): ActionNotificationContent | null {
  const parsed = zSshExecArguments.safeParse(request.arguments);
  if (!parsed.success) return null;
  const { command, host, timeout_seconds, user } = parsed.data;
  return {
    title: actionNotificationTitle(request.title, `Run command on ${user}@${host}`),
    text: timeout_seconds == null ? `$ ${command}` : `$ ${command} · Timeout ${timeout_seconds} s`,
  };
}
