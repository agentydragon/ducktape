import type { ActionRequestView } from "../types";
import { sshExecNotification } from "./ssh/exec";
import type { ActionNotificationContent } from "./types";

type ActionNotificationFormatter = (request: ActionRequestView) => ActionNotificationContent | null;

const FORMATTERS: ReadonlyMap<string, ReadonlyMap<string, ActionNotificationFormatter>> = new Map([
  ["ssh", new Map([["exec", sshExecNotification]])],
]);

/** Format a pending Action for the OS notification surface. Unknown Actions use only their
 * caller-authored summary and description; arbitrary arguments can be large or sensitive. */
export function formatActionNotification(request: ActionRequestView): ActionNotificationContent {
  const custom = FORMATTERS.get(request.action.group)?.get(request.action.name)?.(request);
  if (custom !== null && custom !== undefined) return custom;
  return {
    title: `${request.title} · ${request.action.group} / ${request.action.name}`,
    body: request.description ?? "Action requires approval",
  };
}
