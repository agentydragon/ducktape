import type { ActionRequestView } from "./types";

export type ActionObject = Record<string, unknown>;

export interface ActionPresentationSpec {
  group: string;
  name: string;
  label: (args: ActionObject) => string;
  /** Fields already expressed by the label and therefore omitted from the opened pane. */
  labelFields?: readonly string[];
  /** A short, additional summary shown below the label in the collapsed pane. */
  summaryFields?: readonly string[];
  /** The legacy renderer had a custom result view for this action. */
  resultLabel?: string;
  notificationText?: (request: ActionRequestView) => string | undefined;
}

function field(args: ActionObject, ...names: string[]): unknown {
  for (const name of names) {
    if (args[name] !== undefined && args[name] !== null) return args[name];
  }
  return undefined;
}

function stringField(args: ActionObject, ...names: string[]): string | undefined {
  const value = field(args, ...names);
  return typeof value === "string" && value.trim() !== "" ? value : undefined;
}

function count(args: ActionObject, key: string): number {
  const value = args[key];
  return Array.isArray(value) ? value.length : 0;
}

function plural(number: number, singular: string, pluralForm = `${singular}s`): string {
  return `${number} ${number === 1 ? singular : pluralForm}`;
}

function commandSummary(request: ActionRequestView): string | undefined {
  const args = request.arguments;
  if (typeof args !== "object" || args === null || Array.isArray(args)) return undefined;
  const record = args as ActionObject;
  const command = stringField(record, "command");
  if (!command) return undefined;
  const timeout = field(record, "timeout_seconds");
  return typeof timeout === "number" ? `$ ${command} · Timeout ${timeout} s` : `$ ${command}`;
}

/**
 * React-free action wording shared by the Action pane and service-worker notifications.
 * This is the migration catalog for the legacy Haku renderers: renderer-specific wording and
 * structured data views are dispatched separately from these labels so the service worker never
 * imports React.
 */
export const ACTION_PRESENTATION_CATALOG: readonly ActionPresentationSpec[] = [
  // kubectl-passthrough-mcp renderers.
  {
    group: "kubernetes_admin",
    name: "resources_create_or_update",
    label: () => "Apply Kubernetes resource",
    summaryFields: ["resource"],
  },
  {
    group: "kubernetes_admin",
    name: "resources_get",
    label: (args) => {
      const kind = stringField(args, "kind") ?? "resource";
      const name = stringField(args, "name");
      const namespace = stringField(args, "namespace");
      return `Get ${kind}${name ? ` ${name}` : ""}${namespace ? ` in namespace ${namespace}` : ""}`;
    },
    labelFields: ["apiVersion", "kind", "name", "namespace"],
    summaryFields: ["apiVersion"],
  },
  {
    group: "kubernetes_admin",
    name: "resources_delete",
    label: (args) => {
      const kind = stringField(args, "kind") ?? "resource";
      const name = stringField(args, "name");
      const namespace = stringField(args, "namespace");
      return `Delete ${kind}${name ? ` ${name}` : ""}${namespace ? ` in namespace ${namespace}` : ""}`;
    },
    labelFields: ["apiVersion", "kind", "name", "namespace"],
    summaryFields: ["gracePeriodSeconds"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_delete",
    label: (args) => {
      const name = stringField(args, "name");
      const namespace = stringField(args, "namespace");
      return `Delete Pod${name ? ` ${name}` : ""}${namespace ? ` in namespace ${namespace}` : ""}`;
    },
    labelFields: ["name", "namespace"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_list_in_namespace",
    label: (args) => `List pods in namespace ${stringField(args, "namespace") ?? "(not specified)"}`,
    labelFields: ["namespace"],
    summaryFields: ["labelSelector", "fieldSelector"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_exec",
    label: (args) => {
      const namespace = stringField(args, "namespace");
      const name = stringField(args, "name");
      return `Run command in ${namespace ? `${namespace}/` : ""}${name ?? "Pod"}`;
    },
    labelFields: ["namespace", "name"],
    summaryFields: ["command", "container"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_log",
    label: (args) => {
      const name = stringField(args, "name");
      const namespace = stringField(args, "namespace");
      return `View logs for Pod${name ? ` ${name}` : ""}${namespace ? ` in namespace ${namespace}` : ""}`;
    },
    labelFields: ["name", "namespace"],
    summaryFields: ["container", "previous", "tail"],
  },
  // Agentplane-native read renderers, retained alongside the Haku migration.
  {
    group: "kubernetes_admin",
    name: "resources_list",
    label: (args) => {
      const kind = stringField(args, "kind");
      const namespace = stringField(args, "namespace");
      return `List ${kind ?? "Kubernetes"} resources${namespace ? ` in namespace ${namespace}` : ""}`;
    },
    labelFields: ["apiVersion", "kind", "namespace"],
    summaryFields: ["labelSelector", "fieldSelector"],
  },
  {
    group: "kubernetes_admin",
    name: "events_list",
    label: (args) =>
      `List events${stringField(args, "namespace") ? ` in namespace ${stringField(args, "namespace")}` : ""}`,
    labelFields: ["namespace"],
    summaryFields: ["fieldSelector"],
  },
  // Gmail renderers present in Haku Console.
  {
    group: "gmail",
    name: "drafts_create",
    label: () => "Draft email",
    summaryFields: ["to", "subject"],
    resultLabel: "Draft created",
  },
  {
    group: "gmail",
    name: "threads_modify_labels",
    label: (args) => `Relabel ${plural(count(args, "thread_ids"), "thread")}`,
    summaryFields: ["thread_ids", "add", "remove"],
  },
  {
    group: "gmail",
    name: "threads_get",
    label: () => "Get Gmail thread",
    summaryFields: ["thread_id"],
    resultLabel: "Gmail thread",
  },
  {
    group: "gmail",
    name: "threads_list",
    label: () => "Search Gmail threads",
    summaryFields: ["q", "max_results"],
    resultLabel: "Gmail threads",
  },
  {
    group: "gmail",
    name: "messages_get",
    label: () => "Get Gmail message",
    summaryFields: ["id"],
    resultLabel: "Gmail message",
  },
  // Google Calendar renderers present in Haku Console.
  {
    group: "google_calendar",
    name: "create_event",
    label: (args) => (count(args, "recurrence") > 0 ? "Create recurring calendar event" : "Create calendar event"),
    summaryFields: ["summary", "start", "end", "location"],
    resultLabel: "Created calendar event",
  },
  {
    group: "google_calendar",
    name: "update_event",
    label: (args) => (count(args, "recurrence") > 0 ? "Update recurring calendar event" : "Update calendar event"),
    summaryFields: ["event_id", "summary", "start", "end"],
    resultLabel: "Updated calendar event",
  },
  {
    group: "google_calendar",
    name: "get_event",
    label: () => "Get calendar event",
    summaryFields: ["event_id", "calendar_id"],
    resultLabel: "Calendar event",
  },
  {
    group: "google_calendar",
    name: "list_events",
    label: () => "List calendar events",
    summaryFields: ["calendar_id", "time_min", "time_max", "query"],
    resultLabel: "Calendar events",
  },
  {
    group: "google_calendar",
    name: "list_event_instances",
    label: () => "List calendar event instances",
    summaryFields: ["recurring_event_id", "calendar_id", "time_min", "time_max"],
    resultLabel: "Calendar event instances",
  },
  {
    group: "google_calendar",
    name: "delete_event",
    label: () => "Delete calendar event",
    summaryFields: ["event_id", "calendar_id"],
  },
  // Grocy renderers present in Haku Console. Agentplane calls this group `grocy_sf`.
  {
    group: "grocy_sf",
    name: "stock_add",
    label: (args) => `Add ${plural(count(args, "items"), "item")} to stock`,
    summaryFields: ["items"],
    resultLabel: "Added to stock",
  },
  {
    group: "grocy_sf",
    name: "stock_consume",
    label: (args) => `Remove ${plural(count(args, "items"), "item")} from stock`,
    summaryFields: ["items"],
  },
  {
    group: "grocy_sf",
    name: "stock_entry_edit",
    label: (args) => `Edit ${plural(count(args, "items"), "stock entry", "stock entries")}`,
    summaryFields: ["items"],
    resultLabel: "Updated stock entries",
  },
  {
    group: "grocy_sf",
    name: "stock_get",
    label: () => "View stock",
    summaryFields: ["products", "locations"],
    resultLabel: "Stock",
  },
  {
    group: "grocy_sf",
    name: "products_list",
    label: () => "List Grocy products",
    summaryFields: ["detail"],
    resultLabel: "Products",
  },
  {
    group: "grocy_sf",
    name: "quantity_units_list",
    label: () => "List quantity units",
    summaryFields: ["detail"],
    resultLabel: "Quantity units",
  },
  {
    group: "grocy_sf",
    name: "get_system_info",
    label: () => "View Grocy system information",
    resultLabel: "System information",
  },
  {
    group: "grocy_sf",
    name: "products_create",
    label: (args) => `Create ${plural(count(args, "items"), "product")}`,
    summaryFields: ["items"],
    resultLabel: "Created products",
  },
  {
    group: "grocy_sf",
    name: "products_edit",
    label: (args) => `Edit ${plural(count(args, "items"), "product")}`,
    summaryFields: ["items"],
  },
  {
    group: "grocy_sf",
    name: "shopping_list_get",
    label: () => "View shopping list",
    summaryFields: ["shopping_list"],
    resultLabel: "Shopping list",
  },
  {
    group: "grocy_sf",
    name: "shopping_list_items_add",
    label: (args) => `Add ${plural(count(args, "items"), "item")} to shopping list`,
    summaryFields: ["shopping_list", "items"],
    resultLabel: "Added shopping-list items",
  },
  {
    group: "grocy_sf",
    name: "shopping_list_items_remove",
    label: (args) => `Remove ${plural(count(args, "item_ids"), "shopping-list item")}`,
    summaryFields: ["item_ids"],
    resultLabel: "Removed shopping-list items",
  },
  {
    group: "grocy_sf",
    name: "shopping_list_item_edit",
    label: () => "Edit shopping-list item",
    summaryFields: ["item_id", "amount", "note", "done", "clear_fields"],
  },
  // Tana renderers present in Haku Console.
  {
    group: "tana",
    name: "import_tana_paste",
    label: () => "Import content into Tana",
    summaryFields: ["content", "parentNodeId"],
  },
  {
    group: "tana",
    name: "get_or_create_calendar_node",
    label: () => "Get or create Tana calendar node",
    summaryFields: ["granularity", "date", "workspaceId"],
  },
  { group: "tana", name: "trash_node", label: () => "Move Tana node to trash", summaryFields: ["nodeId"] },
  { group: "tana", name: "edit_node", label: () => "Edit Tana node", summaryFields: ["nodeId", "name", "description"] },
  {
    group: "tana",
    name: "move_node",
    label: () => "Move Tana node",
    summaryFields: ["nodeId", "targetNodeId", "position"],
  },
  {
    group: "tana",
    name: "set_field_option",
    label: (args) => `Tana ${stringField(args, "mode") === "append" ? "append" : "set"} field option`,
    summaryFields: ["nodeId", "attributeId", "optionId", "mode"],
  },
  // Agentplane-native presentations without an equivalent Haku renderer.
  {
    group: "github",
    name: "create_pull_request",
    label: () => "Create pull request",
    summaryFields: ["owner", "repo", "title", "head", "base"],
  },
  {
    group: "ssh",
    name: "exec",
    label: () => "Run SSH command",
    summaryFields: ["command", "timeout_seconds"],
    resultLabel: "SSH command result",
    notificationText: commandSummary,
  },
  { group: "ssh", name: "list_targets", label: () => "List SSH targets", resultLabel: "SSH targets" },
];

const BY_GROUP = new Map<string, Map<string, ActionPresentationSpec>>();
for (const spec of ACTION_PRESENTATION_CATALOG) {
  let group = BY_GROUP.get(spec.group);
  if (group === undefined) {
    group = new Map();
    BY_GROUP.set(spec.group, group);
  }
  group.set(spec.name, spec);
}

export function actionPresentationSpec(action: { group: string; name: string }): ActionPresentationSpec | undefined {
  return BY_GROUP.get(action.group)?.get(action.name);
}

export function actionPresentationLabel(action: { group: string; name: string }, args: unknown): string | null {
  const spec = actionPresentationSpec(action);
  if (spec === undefined || typeof args !== "object" || args === null || Array.isArray(args)) return null;
  return spec.label(args as ActionObject);
}

function normalizedLabel(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

export function isActionTitleRedundant(action: { group: string; name: string }, args: unknown, title: string): boolean {
  const label = actionPresentationLabel(action, args);
  return label !== null && normalizedLabel(title) === normalizedLabel(label);
}

export function actionNotificationTitle(title: string, label: string): string {
  const requestTitle = title.trim();
  if (requestTitle === "") return label;
  if (normalizedLabel(requestTitle) === normalizedLabel(label)) return requestTitle;
  return `${requestTitle} · ${label}`;
}

/** React-free formatter shared by the service worker's pending-action notification registry. */
export function catalogActionNotification(request: ActionRequestView): { title: string; text: string } | null {
  const spec = actionPresentationSpec(request.action);
  const label = actionPresentationLabel(request.action, request.arguments);
  if (spec === undefined || label === null) return null;
  return {
    title: actionNotificationTitle(request.title, label),
    text: spec.notificationText?.(request) ?? request.description ?? "Action requires approval",
  };
}
