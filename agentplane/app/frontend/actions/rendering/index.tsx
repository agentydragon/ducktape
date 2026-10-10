// How an Action's call draws for the operator. Widgets for particular Actions are registered by the
// Action's identity `(group, name)`: one for its request (the arguments), one for its response (the
// value its tool returned), or both. The group is the name the Action Service configures a backend
// under, so an entry follows that name rather than the server's own. Add a module in the
// matching group directory and a row in REGISTRY. Rendering validates the arguments and
// falls back where no widget matches; inline approval is an independent, explicit opt-in.
import type { ReactNode } from "react";

import { type CallToolResult, CallToolResultView, toolValue } from "../call_tool_result";
import type { ActionRequestView } from "../client";
import { renderPreview, type ArgumentsPreview } from "./entry";
import { canApprovePullRequestInline, createPullRequestCompact } from "./github/create_pull_request";
import { eventsListCompact } from "./kubernetes_admin/events_list";
import { podsInNamespaceCompact, podsInNamespacePreview } from "./kubernetes_admin/pods_list_in_namespace";
import { podsLogCompact, podsLogPreview } from "./kubernetes_admin/pods_log";
import { resourcesDeleteCompact } from "./kubernetes_admin/resources_delete";
import { resourcesGetCompact, resourcesGetPreview } from "./kubernetes_admin/resources_get";
import { resourcesListCompact } from "./kubernetes_admin/resources_list";
import { renderResultPreview, type ResultPreview } from "./result_entry";
import { execArgumentsPreview, execResultPreview } from "./ssh/exec";

type ActionIdentity = ActionRequestView["action"];

interface ActionRendering {
  arguments?: ArgumentsPreview;
  result?: ResultPreview;
  /** Compact rendering is independent of whether an inline decision is safe. The schema must
   * reject arguments the widget does not show; otherwise the strip falls back to Review. */
  compact?: ArgumentsPreview;
  /** Opt-in decision, based on the call content. Only consulted after compact.schema parses. */
  canApproveInline?: (args: unknown) => boolean;
}

// Maps rather than object literals, so no group or Action name reaches `Object.prototype`.
const REGISTRY: ReadonlyMap<string, ReadonlyMap<string, ActionRendering>> = new Map([
  [
    "kubernetes_admin",
    new Map<string, ActionRendering>([
      [
        "pods_list_in_namespace",
        { arguments: podsInNamespacePreview, compact: podsInNamespaceCompact, canApproveInline: () => true },
      ],
      ["resources_get", { arguments: resourcesGetPreview, compact: resourcesGetCompact, canApproveInline: () => true }],
      ["resources_list", { compact: resourcesListCompact, canApproveInline: () => true }],
      ["resources_delete", { compact: resourcesDeleteCompact, canApproveInline: () => true }],
      ["pods_log", { arguments: podsLogPreview, compact: podsLogCompact, canApproveInline: () => true }],
      ["events_list", { compact: eventsListCompact, canApproveInline: () => true }],
    ]),
  ],
  [
    "github",
    new Map<string, ActionRendering>([
      ["create_pull_request", { compact: createPullRequestCompact, canApproveInline: canApprovePullRequestInline }],
    ]),
  ],
  // x/ssh_mcp_server/server.py, under the group name staging configures it as.
  ["ssh", new Map<string, ActionRendering>([["exec", { arguments: execArgumentsPreview, result: execResultPreview }]])],
]);

/** An Action's arguments drawn by its own widget when its schema parses them; otherwise `null`, which
 * every card shows as their JSON. The fallback is chosen here rather than by the cards, so a
 * rendering of arguments no widget takes can replace that `null` without them changing. */
export function renderArguments(action: ActionIdentity, args: unknown): ReactNode | null {
  const preview = REGISTRY.get(action.group)?.get(action.name)?.arguments;
  return preview ? renderPreview(preview, args) : null;
}

/** An MCP tool's stored result drawn by the Action's own widget when its schema parses the tool's
 * value; otherwise the way the tool answered. */
export function renderMcpResult(action: ActionIdentity, result: CallToolResult): ReactNode {
  const preview = REGISTRY.get(action.group)?.get(action.name)?.result;
  const value = toolValue(result);
  const drawn = preview && value !== undefined ? renderResultPreview(preview, value) : null;
  return drawn ?? <CallToolResultView result={result} />;
}

/** Inline approval requires both an opt-in for these arguments and a compact widget whose strict
 * schema parses the complete call. A custom summary alone never grants a decision control. */
export function canApproveInline(action: ActionIdentity, args: unknown): boolean {
  const entry = REGISTRY.get(action.group)?.get(action.name);
  return entry?.compact?.schema.safeParse(args).success === true && entry.canApproveInline?.(args) === true;
}

/** An Action's strip summary, even when it still requires opening Review before approval. */
export function compactActionArguments(action: ActionIdentity, args: unknown): ReactNode | null {
  const preview = REGISTRY.get(action.group)?.get(action.name)?.compact;
  return preview ? renderPreview(preview, args) : null;
}
