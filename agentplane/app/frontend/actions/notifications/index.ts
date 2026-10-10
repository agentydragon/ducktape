import { actionPresentationLabel, actionPresentationSpec } from "../presentation_catalog";
import type { ActionRequestView } from "../types";
import { podsInNamespaceNotification } from "./kubernetes_admin/pods_list_in_namespace";
import { sshExecNotification } from "./ssh/exec";
import type { ActionNotificationContent, ActionNotificationParts } from "./types";

type ActionNotificationFormatter = (request: ActionRequestView) => ActionNotificationParts | null;

// Keep strict generated schemas for these tools. The text catalog handles other integrations and
// is React-free so the service worker stays lightweight.
const FORMATTERS: ReadonlyMap<string, ReadonlyMap<string, ActionNotificationFormatter>> = new Map([
  ["kubernetes_admin", new Map([["pods_list_in_namespace", podsInNamespaceNotification]])],
  ["ssh", new Map([["exec", sshExecNotification]])],
]);

/** Format a pending Action for the OS notification surface. Unknown Actions use only their
 * caller-authored summary and description; arbitrary arguments can be large or sensitive. */
export function formatActionNotification(request: ActionRequestView): ActionNotificationContent {
  const formatter = FORMATTERS.get(request.action.group)?.get(request.action.name);
  const parts =
    formatter !== undefined
      ? (formatter(request) ?? fallbackParts(request))
      : (catalogParts(request) ?? fallbackParts(request));
  return {
    title: combineNotificationTitle(request.title, parts.actionTitle),
    text: parts.text,
  };
}

function catalogParts(request: ActionRequestView): ActionNotificationParts | null {
  const spec = actionPresentationSpec(request.action);
  const actionTitle = actionPresentationLabel(request.action, request.arguments);
  if (spec === undefined || actionTitle === null) return null;
  return {
    actionTitle,
    text: spec.notificationText?.(request) ?? request.description ?? "Action requires approval",
  };
}

function fallbackParts(request: ActionRequestView): ActionNotificationParts {
  return {
    actionTitle: `${request.action.group} / ${request.action.name}`,
    text: request.description ?? "Action requires approval",
  };
}

function normalizeTitle(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

/** Combine the caller's context with the Action's description exactly once for every notification. */
function combineNotificationTitle(requestTitle: string, actionTitle: string): string {
  const request = requestTitle.trim();
  if (request === "") return actionTitle;
  if (normalizeTitle(request) === normalizeTitle(actionTitle)) return request;
  return `${request} · ${actionTitle}`;
}
