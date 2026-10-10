// React presentation registry for Actions. Each Action can choose a human-facing label, a different
// widget for the collapsed and opened Actions pane, and full-detail arguments and results. The parallel,
// React-free notification registry lives in ../notifications because the service worker imports it.
// The group is the name the Action Service configures a backend under. Unknown identities or
// payloads fall back to the host's generic view.
import type { ReactNode } from "react";

import { type CallToolResult, CallToolResultView, toolValue } from "../call_tool_result";
import type { ActionRequestView } from "../client";
import { renderPreview, type ArgumentsPreview } from "./entry";
import { canApprovePullRequestInline, createPullRequestPane } from "./github/create_pull_request";
import { canQuickApproveEventsList, eventsListPane } from "./kubernetes_admin/events_list";
import {
  canQuickApprovePodsInNamespace,
  podsInNamespaceCollapsed,
  podsInNamespaceLabel,
  podsInNamespacePane,
  podsInNamespacePreview,
  podsInNamespaceTitleIsRedundant,
} from "./kubernetes_admin/pods_list_in_namespace";
import { canQuickApprovePodsLog, podsLogPreview } from "./kubernetes_admin/pods_log";
import { canQuickApproveResourcesDelete, resourcesDeletePane } from "./kubernetes_admin/resources_delete";
import { canQuickApproveResourcesGet, resourcesGetPreview } from "./kubernetes_admin/resources_get";
import { canQuickApproveResourcesList, resourcesListPane } from "./kubernetes_admin/resources_list";
import { renderResultPreview, type ResultPreview } from "./result_entry";
import { execArgumentsPreview, execCollapsedPreview, execResultPreview } from "./ssh/exec";

type ActionIdentity = ActionRequestView["action"];

interface ActionPresentation {
  /** Replaces the host's technical group/name label in the pane and full details header. */
  label?: ArgumentsPreview;
  pane?: {
    collapsed?: ArgumentsPreview;
    opened?: ArgumentsPreview;
    requestTitleIsRedundant?: (title: string, args: unknown) => boolean;
  };
  details?: {
    arguments?: ArgumentsPreview;
    result?: ResultPreview;
  };
}

// Maps rather than object literals, so no group or Action name reaches `Object.prototype`.
const PRESENTATIONS: ReadonlyMap<string, ReadonlyMap<string, ActionPresentation>> = new Map([
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
          details: { arguments: podsInNamespacePreview },
        },
      ],
      ["resources_get", { pane: { opened: resourcesGetPreview }, details: { arguments: resourcesGetPreview } }],
      ["resources_list", { pane: { opened: resourcesListPane } }],
      ["resources_delete", { pane: { opened: resourcesDeletePane } }],
      ["pods_log", { pane: { opened: podsLogPreview }, details: { arguments: podsLogPreview } }],
      ["events_list", { pane: { opened: eventsListPane } }],
    ]),
  ],
  [
    "github",
    new Map<string, ActionPresentation>([["create_pull_request", { pane: { opened: createPullRequestPane } }]]),
  ],
  // x/ssh_mcp_server/server.py, under the group name staging configures it as.
  [
    "ssh",
    new Map<string, ActionPresentation>([
      [
        "exec",
        {
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
  return PRESENTATIONS.get(action.group)?.get(action.name);
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
  return !(presentation(action)?.pane?.requestTitleIsRedundant?.(title, args) ?? false);
}

/** The pretty argument view on the full details page; Raw remains the host's shared exact-JSON view. */
export function renderDetailsArguments(action: ActionIdentity, args: unknown): ReactNode | null {
  const preview = presentation(action)?.details?.arguments;
  return preview ? renderPreview(preview, args) : null;
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
