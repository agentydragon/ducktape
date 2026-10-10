import { catalogActionNotification } from "../presentation_catalog";
import type { ActionRequestView } from "../types";
import { podsInNamespaceNotification } from "./kubernetes_admin/pods_list_in_namespace";
import { sshExecNotification } from "./ssh/exec";
import type { ActionNotificationContent } from "./types";

type ActionNotificationFormatter = (request: ActionRequestView) => ActionNotificationContent | null;

// Keep the strict generated schemas for these security-sensitive local tools. The larger catalog
// below handles migrated integrations and is React-free so the service worker stays lightweight.
const FORMATTERS: ReadonlyMap<string, ReadonlyMap<string, ActionNotificationFormatter>> = new Map([
  ["kubernetes_admin", new Map([["pods_list_in_namespace", podsInNamespaceNotification]])],
  ["ssh", new Map([["exec", sshExecNotification]])],
]);

/** Format a pending Action for the OS notification surface. Unknown Actions use only their
 * caller-authored summary and description; arbitrary arguments can be large or sensitive. */
export function formatActionNotification(request: ActionRequestView): ActionNotificationContent {
  const formatter = FORMATTERS.get(request.action.group)?.get(request.action.name);
  if (formatter !== undefined) {
    const specific = formatter(request);
    if (specific !== null) return specific;
    return fallback(request);
  }
  const custom = catalogActionNotification(request);
  if (custom !== null) return custom;
  return fallback(request);
}

function fallback(request: ActionRequestView): ActionNotificationContent {
  return {
    title: `${request.title} · ${request.action.group} / ${request.action.name}`,
    text: request.description ?? "Action requires approval",
  };
}
