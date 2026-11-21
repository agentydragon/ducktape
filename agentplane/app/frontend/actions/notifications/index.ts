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
import { podsInNamespaceNotification } from "./kubernetes_admin/pods_list_in_namespace";
import { resourcesCreateOrUpdateNotification } from "./kubernetes_admin/resources_create_or_update";
import { resourcesDeleteNotification } from "./kubernetes_admin/resources_delete";
import { resourcesGetNotification } from "./kubernetes_admin/resources_get";
import { resourcesListNotification } from "./kubernetes_admin/resources_list";
import { sandboxExecNotification } from "./sandbox/exec";
import { sshExecNotification } from "./ssh/exec";
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
export const ACTION_NOTIFICATION_FORMATTERS: ReadonlyMap<
  string,
  ReadonlyMap<string, ActionNotificationFormatter>
> = new Map([
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
      ["products_list", fixedActionNotification("List Grocy products")],
      ["quantity_units_list", fixedActionNotification("List quantity units")],
      ["get_system_info", fixedActionNotification("Show Grocy system information")],
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
      ["ha_config_delete_dashboard", fixedActionNotification("Delete Home Assistant dashboard")],
      ["ha_config_delete_dashboard_resource", fixedActionNotification("Delete dashboard resource")],
      ["ha_config_get_automation", fixedActionNotification("Read automation configuration")],
      ["ha_config_get_calendar_events", fixedActionNotification("Read Home Assistant calendar")],
      ["ha_config_get_category", fixedActionNotification("Read Home Assistant categories")],
      ["ha_config_get_dashboard", fixedActionNotification("Read Home Assistant dashboard")],
      ["ha_config_get_label", fixedActionNotification("Read Home Assistant labels")],
      ["ha_config_get_scene", fixedActionNotification("Read scene configuration")],
      ["ha_config_get_script", fixedActionNotification("Read script configuration")],
      ["ha_config_list_dashboard_resources", fixedActionNotification("List dashboard resources")],
      ["ha_config_list_groups", fixedActionNotification("List Home Assistant groups")],
      ["ha_config_list_helpers", fixedActionNotification("List Home Assistant helpers")],
      ["ha_config_remove_automation", fixedActionNotification("Remove automation")],
      ["ha_config_remove_calendar_event", fixedActionNotification("Remove Home Assistant calendar event")],
      ["ha_config_remove_category", fixedActionNotification("Remove Home Assistant category")],
      ["ha_config_remove_group", fixedActionNotification("Remove Home Assistant group")],
      ["ha_config_remove_label", fixedActionNotification("Remove Home Assistant label")],
      ["ha_config_remove_scene", fixedActionNotification("Remove scene")],
      ["ha_config_remove_script", fixedActionNotification("Remove script")],
      ["ha_config_set_automation", fixedActionNotification("Review automation configuration change")],
      ["ha_config_set_calendar_event", fixedActionNotification("Create Home Assistant calendar event")],
      ["ha_config_set_category", fixedActionNotification("Save Home Assistant category")],
      ["ha_config_set_dashboard", fixedActionNotification("Save dashboard configuration")],
      ["ha_config_set_dashboard_resource", fixedActionNotification("Save dashboard resource")],
      ["ha_config_set_group", fixedActionNotification("Save Home Assistant group")],
      ["ha_config_set_helper", fixedActionNotification("Save Home Assistant helper")],
      ["ha_config_set_label", fixedActionNotification("Save Home Assistant label")],
      ["ha_config_set_scene", fixedActionNotification("Save scene configuration")],
      ["ha_config_set_script", fixedActionNotification("Save script configuration")],
      ["ha_eval_template", fixedActionNotification("Evaluate Home Assistant template")],
      ["ha_get_app", fixedActionNotification("Read Home Assistant apps")],
      ["ha_get_automation_traces", fixedActionNotification("Read automation traces")],
      ["ha_get_blueprint", fixedActionNotification("Read Home Assistant blueprint")],
      ["ha_get_camera_image", fixedActionNotification("Capture Home Assistant camera image")],
      ["ha_get_entity_exposure", fixedActionNotification("Read voice assistant exposure")],
      ["ha_get_hacs_info", fixedActionNotification("Read HACS repository information")],
      ["ha_get_history", fixedActionNotification("Read Home Assistant history")],
      ["ha_get_integration", fixedActionNotification("Read Home Assistant integration")],
      ["ha_get_logs", fixedActionNotification("Read Home Assistant logs")],
      ["ha_get_operation_status", fixedActionNotification("Check Home Assistant operation status")],
      ["ha_get_skill_guide", fixedActionNotification("Read Home Assistant guide")],
      ["ha_get_system_health", fixedActionNotification("Read Home Assistant health")],
      ["ha_get_todo", fixedActionNotification("Read Home Assistant to-do lists")],
      ["ha_get_zone", fixedActionNotification("Read Home Assistant zones")],
      ["ha_import_blueprint", fixedActionNotification("Import Home Assistant blueprint")],
      ["ha_manage_app", fixedActionNotification("Manage Home Assistant Supervisor app")],
      ["ha_manage_backup", fixedActionNotification("Manage Home Assistant backup")],
      ["ha_manage_energy_prefs", fixedActionNotification("Manage Home Assistant energy preferences")],
      ["ha_manage_hacs", fixedActionNotification("Manage HACS repository")],
      ["ha_manage_pipeline", fixedActionNotification("Manage Home Assistant voice pipeline")],
      ["ha_manage_radio", fixedActionNotification("Manage Home Assistant radio")],
      ["ha_manage_theme", fixedActionNotification("Manage Home Assistant theme")],
      ["ha_manage_updates", fixedActionNotification("Manage Home Assistant updates")],
      ["ha_reload_core", fixedActionNotification("Reload Home Assistant components")],
      ["ha_remove_area_or_floor", fixedActionNotification("Remove Home Assistant area or floor")],
      ["ha_remove_device", fixedActionNotification("Remove Home Assistant device")],
      ["ha_remove_entity", fixedActionNotification("Remove Home Assistant entity")],
      ["ha_remove_helpers_integrations", fixedActionNotification("Remove Home Assistant helper or integration")],
      ["ha_remove_todo_item", fixedActionNotification("Remove Home Assistant to-do item")],
      ["ha_remove_zone", fixedActionNotification("Remove Home Assistant zone")],
      ["ha_report_issue", fixedActionNotification("Prepare Home Assistant diagnostic report")],
      ["ha_restart", fixedActionNotification("Review Home Assistant restart")],
      ["ha_set_area_or_floor", fixedActionNotification("Save Home Assistant area or floor")],
      ["ha_set_device", fixedActionNotification("Update Home Assistant device")],
      ["ha_set_entity", fixedActionNotification("Update Home Assistant entity")],
      ["ha_set_integration", fixedActionNotification("Review Home Assistant integration change")],
      ["ha_set_todo_item", fixedActionNotification("Change Home Assistant to-do item")],
      ["ha_set_zone", fixedActionNotification("Save Home Assistant zone")],
    ]),
  ],
  [
    "google_calendar",
    new Map([
      ["create_event", fixedActionNotification("Create Google Calendar event")],
      ["delete_event", fixedActionNotification("Delete Google Calendar event")],
      ["get_event", fixedActionNotification("Read Google Calendar event")],
      ["list_event_instances", fixedActionNotification("List Google Calendar event occurrences")],
      ["list_events", fixedActionNotification("Read Google Calendar agenda")],
      ["update_event", fixedActionNotification("Update Google Calendar event")],
    ]),
  ],
  [
    "orchestration_v1",
    new Map([
      ["change_model", fixedActionNotification("Change Sandbox Session model")],
      ["change_reasoning_effort", fixedActionNotification("Change Sandbox Session reasoning effort")],
      ["create_session", fixedActionNotification("Create Session in Sandbox")],
      ["get_session", fixedActionNotification("Inspect Sandbox Session")],
      ["interrupt_turn", fixedActionNotification("Interrupt Sandbox Session turn")],
      ["launch_sandbox", fixedActionNotification("Launch Session Sandbox")],
      ["list_launch_presets", fixedActionNotification("List launch presets")],
      ["list_sessions", fixedActionNotification("List Sandbox Sessions")],
      ["read_turns", fixedActionNotification("Read Sandbox Session turns")],
      ["submit_input", fixedActionNotification("Submit Sandbox Session input")],
      ["wait_for_turn", fixedActionNotification("Wait for Sandbox Session turn")],
    ]),
  ],
  [
    "sandbox",
    new Map([
      ["exec", sandboxExecNotification],
      ["create", fixedActionNotification("Create Sandbox")],
      ["dispose", fixedActionNotification("Dispose Sandbox")],
      ["get", fixedActionNotification("Inspect Sandbox")],
      ["get_template", fixedActionNotification("Inspect Sandbox template")],
      ["list", fixedActionNotification("List Sandboxes")],
    ]),
  ],
  [
    "ssh",
    new Map([
      ["exec", sshExecNotification],
      ["list_targets", fixedActionNotification("List SSH targets")],
    ]),
  ],
  [
    "tana",
    new Map([
      ["get_or_create_calendar_node", fixedActionNotification("Get or create calendar node")],
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
  const parts =
    ACTION_NOTIFICATION_FORMATTERS.get(action.group)?.get(action.name)?.(request, names) ?? fallbackParts(request);
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
