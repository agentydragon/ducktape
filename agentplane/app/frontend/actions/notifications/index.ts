import type { ActionRequest } from "../../../../action_service/api/contracts/action_service_pb";
import { actionArguments, actionIdentity } from "../presentation";
import { tanaTitles } from "../schemas/tana_titles";
import { createBranchNotification } from "./github/create_branch";
import { createPullRequestNotification } from "./github/create_pull_request";
import { pushFilesNotification } from "./github/push_files";
import { updatePullRequestNotification } from "./github/update_pull_request";
import { eventsListNotification } from "./kubernetes_admin/events_list";
import { podsDeleteNotification } from "./kubernetes_admin/pods_delete";
import { podsExecNotification } from "./kubernetes_admin/pods_exec";
import { podsGetNotification } from "./kubernetes_admin/pods_get";
import { podsLogNotification } from "./kubernetes_admin/pods_log";
import { grocySystemInfoNotification } from "./grocy/get_system_info";
import { grocyProductsListNotification } from "./grocy/products_list";
import { grocyQuantityUnitsListNotification } from "./grocy/quantity_units_list";
import { podsInNamespaceNotification } from "./kubernetes_admin/pods_list_in_namespace";
import { resourcesCreateOrUpdateNotification } from "./kubernetes_admin/resources_create_or_update";
import { resourcesDeleteNotification } from "./kubernetes_admin/resources_delete";
import { resourcesGetNotification } from "./kubernetes_admin/resources_get";
import { resourcesListNotification } from "./kubernetes_admin/resources_list";
import { sandboxExecNotification } from "./sandbox/exec";
import { sshExecNotification } from "./ssh/exec";
import { tanaCalendarNodeNotification } from "./tana/get_or_create_calendar_node";
import { gmailThreadLabelsNotification, tanaMoveNodeNotification } from "./integration_previews";
import type { ActionNotificationContent, ActionNotificationParts } from "./types";

export { notificationPreviewKind } from "./integration_previews";

type ActionNotificationFormatter = (
  request: ActionRequest,
  names?: ReadonlyMap<string, string>
) => ActionNotificationParts | null;

function fixedActionNotification(actionTitle: string): ActionNotificationFormatter {
  return (request) => ({
    actionTitle,
    text: request.description ?? "Action requires approval",
  });
}

function environmentTargetNotification(actionTitle: string): ActionNotificationFormatter {
  return (request) => {
    const target = actionArguments(request).target;
    const name = target !== null && typeof target === "object" && !Array.isArray(target) ? target.name : undefined;
    return fixedActionNotification(typeof name === "string" && name ? `${actionTitle} ${name}` : actionTitle)(request);
  };
}

// This registry intentionally stays parallel to the React presentation registry. The service
// worker imports this module, so notification formatters must remain React-free.
const FORMATTERS: ReadonlyMap<string, ReadonlyMap<string, ActionNotificationFormatter>> = new Map([
  [
    "kubernetes_admin",
    new Map([
      ["resources_create_or_update", resourcesCreateOrUpdateNotification],
      ["resources_get", resourcesGetNotification],
      ["resources_delete", resourcesDeleteNotification],
      ["pods_delete", podsDeleteNotification],
      ["pods_list_in_namespace", podsInNamespaceNotification],
      ["pods_exec", podsExecNotification],
      ["pods_get", podsGetNotification],
      ["pods_log", podsLogNotification],
      ["resources_list", resourcesListNotification],
      ["events_list", eventsListNotification],
    ]),
  ],
  [
    "grocy_sf",
    new Map([
      ["products_list", grocyProductsListNotification],
      ["quantity_units_list", grocyQuantityUnitsListNotification],
      ["get_system_info", grocySystemInfoNotification],
    ]),
  ],
  [
    "github",
    new Map([
      ["push_files", pushFilesNotification],
      ["update_pull_request", updatePullRequestNotification],
      ["create_branch", createBranchNotification],
      ["create_pull_request", createPullRequestNotification],
    ]),
  ],
  [
    "gmail",
    new Map([
      ["drafts_create", fixedActionNotification("Gmail: Draft email")],
      ["threads_list", fixedActionNotification("Gmail: Search threads")],
      ["threads_modify_labels", gmailThreadLabelsNotification],
    ]),
  ],
  [
    "environments",
    new Map([
      ["list", fixedActionNotification("List environments")],
      ["get", environmentTargetNotification("Inspect environment")],
      ["resume", environmentTargetNotification("Resume environment")],
    ]),
  ],
  [
    "sessions",
    new Map([
      ["create", fixedActionNotification("Create Session")],
      ["get", fixedActionNotification("Inspect Session")],
      ["submit_input", fixedActionNotification("Submit Session input")],
      ["observe_input", fixedActionNotification("Observe Session input")],
      ["cancel_input", fixedActionNotification("Cancel Session input")],
      ["interrupt_turn", fixedActionNotification("Interrupt Session turn")],
      ["read_turns", fixedActionNotification("Read Session turns")],
    ]),
  ],
  ["sandbox", new Map([["exec", sandboxExecNotification]])],
  ["ssh", new Map([["exec", sshExecNotification]])],
  [
    "tana",
    new Map([
      ["get_or_create_calendar_node", tanaCalendarNodeNotification],
      ["move_node", tanaMoveNodeNotification],
      ...Array.from(tanaTitles, ([name, title]) => [name, fixedActionNotification(title)] as const),
    ]),
  ],
]);

/** Format a pending Action for the OS notification surface. Unknown Actions use only their
 * caller-authored summary and description; arbitrary arguments can be large or sensitive. */
export function formatActionNotification(
  request: ActionRequest,
  names?: ReadonlyMap<string, string>
): ActionNotificationContent {
  const action = actionIdentity(request);
  const parts = FORMATTERS.get(action.group)?.get(action.name)?.(request, names) ?? fallbackParts(request);
  return {
    title: combineNotificationTitle(request.title, parts.actionTitle),
    text: parts.text,
  };
}

function fallbackParts(request: ActionRequest): ActionNotificationParts {
  const action = actionIdentity(request);
  return {
    actionTitle: `${action.group} / ${action.name}`,
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
