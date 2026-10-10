import type { ActionRequestView } from "../types";
import { podsInNamespaceNotification } from "./kubernetes_admin/pods_list_in_namespace";
import { sshExecNotification } from "./ssh/exec";
import type { ActionNotificationContent } from "./types";

type ActionNotificationFormatter = (request: ActionRequestView) => ActionNotificationContent | null;

// This registry intentionally stays parallel to the React presentation registry. The service
// worker imports this module, so notification formatters must remain React-free.
const FORMATTERS: ReadonlyMap<string, ReadonlyMap<string, ActionNotificationFormatter>> = new Map([
  ["kubernetes_admin", new Map([["pods_list_in_namespace", podsInNamespaceNotification]])],
  ["ssh", new Map([["exec", sshExecNotification]])],
]);

/** Format a pending Action for the OS notification surface. Unknown Actions use only their
 * caller-authored summary and description; arbitrary arguments can be large or sensitive. */
export function formatActionNotification(request: ActionRequestView): ActionNotificationContent {
  const custom = FORMATTERS.get(request.action.group)?.get(request.action.name)?.(request);
  if (custom !== null && custom !== undefined) return custom;
  return {
    title: `${request.title} · ${request.action.group} / ${request.action.name}`,
    text: request.description ?? "Action requires approval",
  };
}
