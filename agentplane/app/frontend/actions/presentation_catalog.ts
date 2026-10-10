import type { ActionRequestView } from "./types";

export type ActionObject = Record<string, unknown>;

export interface ActionPresentationSpec {
  group: string;
  name: string;
  label: (args: ActionObject) => string;
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

function kubernetesTarget(kind: string, name: string, namespace: string | undefined): string {
  return `${kind} ${namespace ? `${namespace}/` : ""}${name}`;
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
 * React-free text fallback and notification metadata for Actions. The React registry separately
 * accepts purpose-built DOM renderers for any label, pane, or details slot; this catalog contains
 * no field lists or rendering recipe that every Action must follow.
 */
export const ACTION_PRESENTATION_CATALOG: readonly ActionPresentationSpec[] = [
  // kubectl-passthrough-mcp renderers.
  {
    group: "kubernetes_admin",
    name: "resources_create_or_update",
    label: () => "Apply Kubernetes resource",
  },
  {
    group: "kubernetes_admin",
    name: "resources_get",
    label: (args) => {
      const kind = stringField(args, "kind") ?? "resource";
      const name = stringField(args, "name");
      const namespace = stringField(args, "namespace");
      return `Get ${name ? kubernetesTarget(kind, name, namespace) : kind}`;
    },
  },
  {
    group: "kubernetes_admin",
    name: "resources_delete",
    label: (args) => {
      const kind = stringField(args, "kind") ?? "resource";
      const name = stringField(args, "name");
      const namespace = stringField(args, "namespace");
      return `Delete ${name ? kubernetesTarget(kind, name, namespace) : kind}`;
    },
  },
  {
    group: "kubernetes_admin",
    name: "pods_delete",
    label: (args) => {
      const name = stringField(args, "name");
      const namespace = stringField(args, "namespace");
      return `Delete ${name ? kubernetesTarget("Pod", name, namespace) : "Pod"}`;
    },
  },
  {
    group: "kubernetes_admin",
    name: "pods_list_in_namespace",
    label: (args) => `List pods in namespace ${stringField(args, "namespace") ?? "(not specified)"}`,
  },
  {
    group: "kubernetes_admin",
    name: "pods_exec",
    label: (args) => {
      const namespace = stringField(args, "namespace");
      const name = stringField(args, "name");
      return `Run command in ${name ? kubernetesTarget("Pod", name, namespace) : "Pod"}`;
    },
  },
  {
    group: "kubernetes_admin",
    name: "pods_log",
    label: (args) => {
      const name = stringField(args, "name");
      const namespace = stringField(args, "namespace");
      return `View logs for ${name ? kubernetesTarget("Pod", name, namespace) : "Pod"}`;
    },
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
  },
  {
    group: "kubernetes_admin",
    name: "events_list",
    label: (args) =>
      `List events${stringField(args, "namespace") ? ` in namespace ${stringField(args, "namespace")}` : ""}`,
  },
  // No-lookup Gmail renderers present in Haku Console.
  {
    group: "gmail",
    name: "drafts_create",
    label: (args) => `Draft email${stringField(args, "subject") ? `: ${stringField(args, "subject")}` : ""}`,
    resultLabel: "Draft created",
  },
  {
    group: "gmail",
    name: "threads_list",
    label: () => "Search Gmail threads",
    resultLabel: "Gmail threads",
  },
  // No-lookup Grocy renderers present in Haku Console. Agentplane calls this group `grocy_sf`.
  {
    group: "grocy_sf",
    name: "products_list",
    label: () => "List Grocy products",
    resultLabel: "Products",
  },
  {
    group: "grocy_sf",
    name: "quantity_units_list",
    label: () => "List quantity units",
    resultLabel: "Quantity units",
  },
  {
    group: "grocy_sf",
    name: "get_system_info",
    label: () => "View Grocy system information",
    resultLabel: "System information",
  },
  // No-lookup Tana renderers present in Haku Console.
  {
    group: "tana",
    name: "get_or_create_calendar_node",
    label: () => "Get or create Tana calendar node",
  },
  // Agentplane-native presentations without an equivalent Haku renderer.
  {
    group: "github",
    name: "create_pull_request",
    label: () => "Create pull request",
  },
  {
    group: "ssh",
    name: "exec",
    label: () => "Run SSH command",
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
