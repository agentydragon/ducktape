import type { ActionRequestView } from "../../types";
import { zSshExecArguments } from "../../schemas/ssh/exec";
import type { ActionNotificationContent } from "../types";

export function sshExecNotification(request: ActionRequestView): ActionNotificationContent | null {
  const parsed = zSshExecArguments.safeParse(request.arguments);
  if (!parsed.success) return null;
  const { command, host, timeout_seconds, user } = parsed.data;
  return {
    title: `${request.title} · SSH exec · ${user}@${host}`,
    text: timeout_seconds == null ? `$ ${command}` : `$ ${command} · Timeout ${timeout_seconds} s`,
  };
}
