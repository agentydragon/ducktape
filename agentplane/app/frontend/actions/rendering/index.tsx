// React presentation registry for Actions. Each Action can choose a human-facing label, a different
// widget for the collapsed and opened Actions pane, and full-detail arguments and results. The parallel,
// React-free notification registry lives in ../notifications because the service worker imports it.
// The group is the name the Action Service configures a backend under. Unknown identities or
// payloads fall back to the host's generic view.
import type { ReactNode } from "react";

import { type CallToolResult, CallToolResultView, toolValue } from "../call_tool_result";
import type { ActionRequestView } from "../client";
import { renderPreview, type ArgumentsPreview, type CallPreview } from "./entry";
import {
  canApprovePullRequestInline,
  createPullRequestLabel,
  createPullRequestPane,
} from "./github/create_pull_request";
import {
  gmailDraftCollapsed,
  gmailDraftCall,
  gmailDraftDetails,
  gmailDraftLabel,
  gmailDraftOpened,
  gmailDraftResult,
  gmailThreadSearchCollapsed,
  gmailThreadSearchLabel,
  gmailThreadSearchOpened,
  gmailThreadsResult,
} from "./gmail";
import {
  productsListLabel,
  productsListArguments,
  productsListResult,
  quantityUnitsListLabel,
  quantityUnitsListArguments,
  quantityUnitsListResult,
  systemInfoLabel,
  systemInfoArguments,
  systemInfoResult,
} from "./grocy";
import { canQuickApproveEventsList, eventsListLabel, eventsListPane } from "./kubernetes_admin/events_list";
import {
  canQuickApprovePodsInNamespace,
  podsInNamespaceCollapsed,
  podsInNamespaceDetails,
  podsInNamespaceLabel,
  podsInNamespacePane,
  podsInNamespaceTitleIsRedundant,
} from "./kubernetes_admin/pods_list_in_namespace";
import {
  podsDeleteLabel,
  podsDeletePane,
  podsExecCollapsed,
  podsExecLabel,
  podsExecPane,
} from "./kubernetes_admin/pods_exec";
import { canQuickApprovePodsLog, podsLogLabel, podsLogPreview } from "./kubernetes_admin/pods_log";
import {
  resourcesApplyCollapsed,
  resourcesApplyLabel,
  resourcesApplyManifest,
} from "./kubernetes_admin/resources_apply";
import {
  canQuickApproveResourcesDelete,
  resourcesDeleteLabel,
  resourcesDeletePane,
} from "./kubernetes_admin/resources_delete";
import { canQuickApproveResourcesGet, resourcesGetLabel, resourcesGetPane } from "./kubernetes_admin/resources_get";
import { canQuickApproveResourcesList, resourcesListLabel, resourcesListPane } from "./kubernetes_admin/resources_list";
import { renderResultPreview, type ResultPreview } from "./result_entry";
import { execArgumentsPreview, execCollapsedPreview, execLabel, execResultPreview } from "./ssh/exec";
import { calendarNodeArgumentsPreview, calendarNodeLabel } from "./tana";

type ActionIdentity = ActionRequestView["action"];

interface ActionPresentation {
  /** Omit a slot for the generic fallback, set null to suppress it, or provide its own React DOM. */
  label?: ArgumentsPreview | null;
  /** A custom view that keeps an Action's arguments and returned value together. */
  call?: CallPreview;
  pane?: {
    collapsed?: ArgumentsPreview | null;
    opened?: ArgumentsPreview | null;
    requestTitleIsRedundant?: (title: string, args: unknown) => boolean;
  };
  details?: {
    arguments?: ArgumentsPreview | null;
    result?: ResultPreview | null;
  };
}

// React-only per-Action registry. Entries own arbitrary DOM for the slots they implement; they do
// not describe data fields for a shared renderer. Maps avoid prototype-key lookups for Action names.
const ACTION_RENDERERS: ReadonlyMap<string, ReadonlyMap<string, ActionPresentation>> = new Map([
  [
    "kubernetes_admin",
    new Map<string, ActionPresentation>([
      [
        "pods_list_in_namespace",
        {
          label: podsInNamespaceLabel,
          pane: {
            collapsed: podsInNamespaceCollapsed,
            opened: podsInNamespacePane,
            requestTitleIsRedundant: podsInNamespaceTitleIsRedundant,
          },
          details: { arguments: podsInNamespaceDetails },
        },
      ],
      [
        "resources_create_or_update",
        {
          label: resourcesApplyLabel,
          pane: { collapsed: resourcesApplyCollapsed, opened: resourcesApplyManifest },
          details: { arguments: resourcesApplyManifest },
        },
      ],
      [
        "pods_delete",
        { label: podsDeleteLabel, pane: { opened: podsDeletePane }, details: { arguments: podsDeletePane } },
      ],
      [
        "pods_exec",
        {
          label: podsExecLabel,
          pane: { collapsed: podsExecCollapsed, opened: podsExecPane },
          details: { arguments: podsExecPane },
        },
      ],
      [
        "resources_get",
        { label: resourcesGetLabel, pane: { opened: resourcesGetPane }, details: { arguments: resourcesGetPane } },
      ],
      [
        "resources_list",
        { label: resourcesListLabel, pane: { opened: resourcesListPane }, details: { arguments: resourcesListPane } },
      ],
      [
        "resources_delete",
        {
          label: resourcesDeleteLabel,
          pane: { opened: resourcesDeletePane },
          details: { arguments: resourcesDeletePane },
        },
      ],
      ["pods_log", { label: podsLogLabel, pane: { opened: podsLogPreview }, details: { arguments: podsLogPreview } }],
      [
        "events_list",
        { label: eventsListLabel, pane: { opened: eventsListPane }, details: { arguments: eventsListPane } },
      ],
    ]),
  ],
  [
    "github",
    new Map<string, ActionPresentation>([
      [
        "create_pull_request",
        {
          label: createPullRequestLabel,
          pane: { opened: createPullRequestPane },
          details: { arguments: createPullRequestPane },
        },
      ],
    ]),
  ],
  [
    "gmail",
    // These static views need no Gmail read lookup. Subject/label resolution for thread and message
    // actions stays on the submitted-data fallback until Agentplane has an explicit lookup surface.
    new Map<string, ActionPresentation>([
      [
        "drafts_create",
        {
          label: gmailDraftLabel,
          call: gmailDraftCall,
          pane: { collapsed: gmailDraftCollapsed, opened: gmailDraftOpened },
          details: { arguments: gmailDraftDetails, result: gmailDraftResult },
        },
      ],
      [
        "threads_list",
        {
          label: gmailThreadSearchLabel,
          pane: { collapsed: gmailThreadSearchCollapsed, opened: gmailThreadSearchOpened },
          details: { arguments: gmailThreadSearchOpened, result: gmailThreadsResult },
        },
      ],
    ]),
  ],
  [
    "grocy_sf",
    // Only actions whose Haku renderers need no lookups are registered here. Lookup-backed Grocy
    // actions are absent entirely and use Agentplane's unported generic presentation.
    new Map<string, ActionPresentation>([
      [
        "products_list",
        {
          label: productsListLabel,
          pane: { opened: productsListArguments },
          details: { arguments: productsListArguments, result: productsListResult },
        },
      ],
      [
        "quantity_units_list",
        {
          label: quantityUnitsListLabel,
          pane: { opened: quantityUnitsListArguments },
          details: { arguments: quantityUnitsListArguments, result: quantityUnitsListResult },
        },
      ],
      [
        "get_system_info",
        {
          label: systemInfoLabel,
          pane: { opened: systemInfoArguments },
          details: { arguments: systemInfoArguments, result: systemInfoResult },
        },
      ],
    ]),
  ],
  [
    "tana",
    new Map<string, ActionPresentation>([
      [
        "get_or_create_calendar_node",
        {
          label: calendarNodeLabel,
          pane: { opened: calendarNodeArgumentsPreview },
          details: { arguments: calendarNodeArgumentsPreview },
        },
      ],
    ]),
  ],
  // x/ssh_mcp_server/server.py, under the group name staging configures it as.
  [
    "ssh",
    new Map<string, ActionPresentation>([
      [
        "exec",
        {
          label: execLabel,
          pane: { collapsed: execCollapsedPreview, opened: execArgumentsPreview },
          details: { arguments: execArgumentsPreview, result: execResultPreview },
        },
      ],
    ]),
  ],
]);

// Quick approval is deliberately a separate capability: a custom pane renderer does not grant an
// inline decision. Each entry validates the complete Action arguments independently of its views.
const QUICK_APPROVALS: ReadonlyMap<string, ReadonlyMap<string, (args: unknown) => boolean>> = new Map([
  [
    "kubernetes_admin",
    new Map([
      ["pods_list_in_namespace", canQuickApprovePodsInNamespace],
      ["resources_get", canQuickApproveResourcesGet],
      ["resources_list", canQuickApproveResourcesList],
      ["resources_delete", canQuickApproveResourcesDelete],
      ["pods_log", canQuickApprovePodsLog],
      ["events_list", canQuickApproveEventsList],
    ]),
  ],
  ["github", new Map([["create_pull_request", canApprovePullRequestInline]])],
]);

function presentation(action: ActionIdentity): ActionPresentation | undefined {
  return ACTION_RENDERERS.get(action.group)?.get(action.name);
}

/** An Action's human-facing name; `null` keeps the host's group/name label. */
export function renderActionLabel(action: ActionIdentity, args: unknown): ReactNode | null {
  const label = presentation(action)?.label;
  return label ? renderPreview(label, args) : null;
}

/** An Action's collapsed-pane summary; `null` leaves the shared caller title in place. */
export function renderPaneCollapsed(action: ActionIdentity, args: unknown): ReactNode | null {
  const preview = presentation(action)?.pane?.collapsed;
  return preview ? renderPreview(preview, args) : null;
}

/** An Action's opened-pane view; `null` asks the host to direct the operator to full details. */
export function renderPaneOpened(action: ActionIdentity, args: unknown): ReactNode | null {
  const preview = presentation(action)?.pane?.opened;
  return preview ? renderPreview(preview, args) : null;
}

/** Whether the caller's title repeats the Action label shown in the pane heading. */
export function shouldRenderPaneRequestTitle(action: ActionIdentity, args: unknown, title: string): boolean {
  const customCheck = presentation(action)?.pane?.requestTitleIsRedundant;
  return !(customCheck?.(title, args) ?? false);
}

/** The pretty argument view on the full details page; Raw remains the host's shared exact-JSON view. */
export function renderDetailsArguments(action: ActionIdentity, args: unknown): ReactNode | null {
  const preview = presentation(action)?.details?.arguments;
  return preview ? renderPreview(preview, args) : null;
}

export interface RenderedActionCall {
  content: ReactNode;
  /** Whether this view includes a successfully parsed result and can replace the separate result block. */
  includesResult: boolean;
}

/** A combined per-Action request/result view, or `null` when its action-local schemas do not match. */
export function renderActionCall(
  action: ActionIdentity,
  args: unknown,
  result?: CallToolResult
): RenderedActionCall | null {
  const preview = presentation(action)?.call;
  if (!preview) return null;
  const parsedArgs = preview.argumentSchema.safeParse(args);
  if (!parsedArgs.success) return null;

  if (result === undefined || result.isError) {
    return { content: preview.render(parsedArgs.data as never, undefined), includesResult: false };
  }

  const value = toolValue(result);
  if (value === undefined) return null;
  const parsedResult = preview.resultSchema.safeParse(value);
  if (!parsedResult.success) return null;
  return { content: preview.render(parsedArgs.data as never, parsedResult.data as never), includesResult: true };
}

/** An MCP result's action-specific details view, or the generic view of the tool's response. */
export function renderDetailsResult(action: ActionIdentity, result: CallToolResult): ReactNode {
  const preview = presentation(action)?.details?.result;
  const value = toolValue(result);
  const drawn = preview && value !== undefined ? renderResultPreview(preview, value) : null;
  return drawn ?? <CallToolResultView result={result} />;
}

/** Inline approval is an explicit action capability, independent of the presentation chosen. */
export function canApproveInline(action: ActionIdentity, args: unknown): boolean {
  return QUICK_APPROVALS.get(action.group)?.get(action.name)?.(args) ?? false;
}
