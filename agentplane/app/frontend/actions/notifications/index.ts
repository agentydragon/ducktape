import type { ActionRequestView } from "../types";
import { grocySystemInfoNotification } from "./grocy/get_system_info";
import { grocyProductsListNotification } from "./grocy/products_list";
import { grocyQuantityUnitsListNotification } from "./grocy/quantity_units_list";
import { podsInNamespaceNotification } from "./kubernetes_admin/pods_list_in_namespace";
import { sshExecNotification } from "./ssh/exec";
import { tanaCalendarNodeNotification } from "./tana/get_or_create_calendar_node";
import type { ActionNotificationContent, ActionNotificationParts } from "./types";

type ActionNotificationFormatter = (request: ActionRequestView) => ActionNotificationParts | null;

// This registry intentionally stays parallel to the React presentation registry. The service
// worker imports this module, so notification formatters must remain React-free.
const FORMATTERS: ReadonlyMap<string, ReadonlyMap<string, ActionNotificationFormatter>> = new Map([
  ["kubernetes_admin", new Map([["pods_list_in_namespace", podsInNamespaceNotification]])],
  [
    "grocy_sf",
    new Map([
      ["products_list", grocyProductsListNotification],
      ["quantity_units_list", grocyQuantityUnitsListNotification],
      ["get_system_info", grocySystemInfoNotification],
    ]),
  ],
  ["ssh", new Map([["exec", sshExecNotification]])],
  ["tana", new Map([["get_or_create_calendar_node", tanaCalendarNodeNotification]])],
]);

/** Format a pending Action for the OS notification surface. Unknown Actions use only their
 * caller-authored summary and description; arbitrary arguments can be large or sensitive. */
export function formatActionNotification(request: ActionRequestView): ActionNotificationContent {
  const parts = FORMATTERS.get(request.action.group)?.get(request.action.name)?.(request) ?? fallbackParts(request);
  return {
    title: combineNotificationTitle(request.title, parts.actionTitle),
    text: parts.text,
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

/** Compose caller context and the Action's description once for every notification. */
function combineNotificationTitle(requestTitle: string, actionTitle: string): string {
  const request = requestTitle.trim();
  if (request === "") return actionTitle;
  if (normalizeTitle(request) === normalizeTitle(actionTitle)) return request;
  return `${request} · ${actionTitle}`;
}
