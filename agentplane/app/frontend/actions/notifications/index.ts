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

const resourceScaleNotification: ActionNotificationFormatter = (request) => {
  const args = actionArguments(request);
  const mutation = args !== null && typeof args === "object" && "scale" in args && Number.isSafeInteger(args.scale);
  return fixedActionNotification(mutation ? "Set Kubernetes replica count" : "Review Kubernetes resource scale")(
    request
  );
};

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
      ["pods_list", fixedActionNotification("List Pods across namespaces")],
      ["namespaces_list", fixedActionNotification("List Kubernetes namespaces")],
      ["projects_list", fixedActionNotification("List OpenShift projects")],
      ["nodes_log", fixedActionNotification("View Kubernetes Node logs")],
      ["nodes_stats_summary", fixedActionNotification("Read Kubernetes Node statistics")],
      ["nodes_top", fixedActionNotification("Read Kubernetes Node metrics")],
      ["pods_top", fixedActionNotification("Read Kubernetes Pod metrics")],
      ["pods_run", fixedActionNotification("Run Kubernetes Pod")],
      ["resources_scale", resourceScaleNotification],
      ["configuration_view", fixedActionNotification("Read Kubernetes configuration")],
    ]),
  ],
  [
    "grocy_sf",
    new Map([
      ["products_list", grocyProductsListNotification],
      ["quantity_units_list", grocyQuantityUnitsListNotification],
      ["get_system_info", grocySystemInfoNotification],
      ["products_edit", fixedActionNotification("Edit Grocy products")],
      ["stock_get", fixedActionNotification("View Grocy stock on hand")],
      ["entities_create", fixedActionNotification("Create Grocy records")],
      ["entities_get", fixedActionNotification("Fetch Grocy records by ID")],
      ["entities_list", fixedActionNotification("List Grocy records by type")],
      ["entity_delete", fixedActionNotification("Delete Grocy record")],
      ["entity_update", fixedActionNotification("Update Grocy record")],
      ["file_get", fixedActionNotification("Read Grocy file")],
      ["file_upload", fixedActionNotification("Upload Grocy file")],
      ["get_current_user", fixedActionNotification("View current Grocy user")],
      ["get_db_changed_time", fixedActionNotification("View Grocy database last change")],
      ["products_create", fixedActionNotification("Create Grocy products")],
      ["product_delete", fixedActionNotification("Delete Grocy product")],
      ["products_merge", fixedActionNotification("Merge Grocy products")],
      ["locations_create", fixedActionNotification("Create Grocy locations")],
      ["locations_list", fixedActionNotification("List Grocy storage locations")],
      ["product_groups_create", fixedActionNotification("Create Grocy product groups")],
      ["product_groups_list", fixedActionNotification("List Grocy product groups")],
      ["quantity_units_create", fixedActionNotification("Create Grocy quantity units")],
      ["shopping_list_get", fixedActionNotification("View Grocy shopping list")],
      ["shopping_list_clear", fixedActionNotification("Empty Grocy shopping list")],
      ["shopping_list_items_add", fixedActionNotification("Add Grocy shopping items")],
      ["shopping_list_items_remove", fixedActionNotification("Remove Grocy shopping items")],
      ["shopping_list_item_edit", fixedActionNotification("Edit Grocy shopping item")],
      ["shopping_lists_list", fixedActionNotification("List Grocy shopping lists")],
      ["shopping_lists_create", fixedActionNotification("Create Grocy shopping lists")],
      ["stock_add", fixedActionNotification("Add Grocy stock")],
      ["stock_consume", fixedActionNotification("Consume Grocy stock")],
      ["stock_set", fixedActionNotification("Set Grocy stock totals")],
      ["stock_transfer", fixedActionNotification("Transfer Grocy stock")],
      ["stock_entries_list", fixedActionNotification("View Grocy stock entries")],
      ["stock_entry_edit", fixedActionNotification("Edit Grocy stock entries")],
      ["get_product_stock", fixedActionNotification("View stock for a Grocy product")],
      ["get_expiring_stock", fixedActionNotification("View expiring Grocy stock")],
      ["get_expired_stock", fixedActionNotification("View expired Grocy stock")],
      ["get_below_minimum_stock", fixedActionNotification("View Grocy stock below minimum")],
      ["list_volatile_stock", fixedActionNotification("View Grocy stock needing attention")],
      ["open_product_stock", fixedActionNotification("Mark Grocy stock as opened")],
      ["transaction_undo", fixedActionNotification("Undo Grocy stock transaction")],
    ]),
  ],
  [
    "github",
    new Map([
      ["push_files", pushFilesNotification],
      ["update_pull_request", updatePullRequestNotification],
      ["create_branch", createBranchNotification],
      ["create_pull_request", createPullRequestNotification],
      ["create_or_update_file", fixedActionNotification("Write GitHub file")],
      ["delete_file", fixedActionNotification("Delete GitHub file")],
      ["search_pull_requests", fixedActionNotification("Search GitHub pull requests")],
      ["update_pull_request_branch", fixedActionNotification("Update GitHub pull request branch")],
      ["list_pull_requests", fixedActionNotification("List GitHub pull requests")],
      ["pull_request_read", fixedActionNotification("Read GitHub pull request")],
      ["merge_pull_request", fixedActionNotification("Merge GitHub pull request")],
      ["request_copilot_review", fixedActionNotification("Request Copilot review")],
      ["pull_request_review_write", fixedActionNotification("Change GitHub pull request review")],
      ["add_comment_to_pending_review", fixedActionNotification("Comment on pending GitHub review")],
      ["add_reply_to_pull_request_comment", fixedActionNotification("Reply or react to GitHub review comment")],
      ["issue_read", fixedActionNotification("Read GitHub issue")],
      ["list_issues", fixedActionNotification("List GitHub issues")],
      ["search_issues", fixedActionNotification("Search GitHub issues")],
      ["issue_write", fixedActionNotification("Change GitHub issue")],
      ["sub_issue_write", fixedActionNotification("Change GitHub sub-issue relationship")],
      ["add_issue_comment", fixedActionNotification("Comment or react on GitHub issue")],
      ["update_issue_comment", fixedActionNotification("Edit GitHub issue comment")],
      ["list_issue_fields", fixedActionNotification("List GitHub issue fields")],
      ["list_issue_types", fixedActionNotification("List GitHub issue types")],
      ["get_label", fixedActionNotification("Read GitHub issue label")],
      ["create_repository", fixedActionNotification("Create GitHub repository")],
      ["fork_repository", fixedActionNotification("Fork GitHub repository")],
      ["get_file_contents", fixedActionNotification("Read GitHub repository contents")],
      ["get_commit", fixedActionNotification("Read GitHub commit")],
      ["list_commits", fixedActionNotification("List GitHub commits")],
      ["list_branches", fixedActionNotification("List GitHub branches")],
      ["list_tags", fixedActionNotification("List GitHub tags")],
      ["get_tag", fixedActionNotification("Read GitHub tag")],
      ["list_releases", fixedActionNotification("List GitHub releases")],
      ["get_latest_release", fixedActionNotification("Read latest GitHub release")],
      ["get_release_by_tag", fixedActionNotification("Read GitHub release by tag")],
      ["search_commits", fixedActionNotification("Search GitHub commits")],
      ["get_me", fixedActionNotification("Read authenticated GitHub profile")],
      ["get_team_members", fixedActionNotification("List GitHub team members")],
      ["get_teams", fixedActionNotification("List GitHub teams")],
      ["list_repository_collaborators", fixedActionNotification("List GitHub repository collaborators")],
      ["search_code", fixedActionNotification("Search GitHub code")],
      ["search_repositories", fixedActionNotification("Search GitHub repositories")],
      ["search_users", fixedActionNotification("Search GitHub users")],
      ["actions_get", fixedActionNotification("Read GitHub Actions data")],
      ["actions_list", fixedActionNotification("List GitHub Actions data")],
      ["actions_run_trigger", fixedActionNotification("Change GitHub workflow run")],
      ["get_job_logs", fixedActionNotification("Read GitHub Actions logs")],
      ["ui_get", fixedActionNotification("Read GitHub form options")],
      ["run_secret_scanning", fixedActionNotification("Scan for secrets with GitHub")],
    ]),
  ],
  [
    "gmail",
    new Map([
      ["drafts_create", fixedActionNotification("Gmail: Draft email")],
      ["drafts_delete", fixedActionNotification("Delete Gmail draft")],
      ["drafts_get", fixedActionNotification("Read Gmail draft")],
      ["drafts_list", fixedActionNotification("Search Gmail drafts")],
      ["drafts_update", fixedActionNotification("Replace Gmail draft")],
      ["filters_create", fixedActionNotification("Create Gmail filter")],
      ["filters_delete", fixedActionNotification("Delete Gmail filter")],
      ["filters_get", fixedActionNotification("Read Gmail filter")],
      ["filters_list", fixedActionNotification("List Gmail filters")],
      ["labels_create", fixedActionNotification("Create Gmail label")],
      ["labels_delete", fixedActionNotification("Delete Gmail label")],
      ["labels_get", fixedActionNotification("Read Gmail label")],
      ["labels_list", fixedActionNotification("List Gmail labels")],
      ["labels_patch", fixedActionNotification("Update Gmail label")],
      ["messages_get", fixedActionNotification("Read Gmail message")],
      ["threads_get", fixedActionNotification("Read Gmail thread")],
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
  [
    "home_assistant",
    new Map([
      ["ha_call_service", fixedActionNotification("Call Home Assistant service")],
      ["ha_call_event", fixedActionNotification("Fire Home Assistant event")],
      ["ha_bulk_control", fixedActionNotification("Control Home Assistant devices")],
      ["ha_get_state", fixedActionNotification("Read entity state")],
      ["ha_get_entity", fixedActionNotification("Inspect Home Assistant entity")],
      ["ha_get_device", fixedActionNotification("Inspect Home Assistant devices")],
      ["ha_get_overview", fixedActionNotification("Read Home Assistant overview")],
      ["ha_search", fixedActionNotification("Search Home Assistant")],
      ["ha_list_floors_areas", fixedActionNotification("List floors and areas")],
      ["ha_list_services", fixedActionNotification("List Home Assistant services")],
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
