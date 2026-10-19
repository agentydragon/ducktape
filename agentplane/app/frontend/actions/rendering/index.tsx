// React presentation registry for Actions. Each Action can choose a human-facing label, a different
// widget for the collapsed and opened Actions pane, and full-detail arguments and results. The parallel,
// React-free notification registry lives in ../notifications because the service worker imports it.
// The group is the name the Action Service configures a backend under. Unknown identities or
// payloads fall back to the host's generic view.
import type { ReactNode } from "react";

import { type CallToolResult, CallToolResultView, toolValue } from "../call_tool_result";
import type { ActionRequestView } from "../client";
import { renderPreview } from "./entry";
import type { ActionPresentation } from "./presentation";
import { renderResultPreview } from "./result_entry";
import { kubernetesAdminRenderers } from "./kubernetes_admin/index";
import { githubRenderers } from "./github/index";
import { gmailRenderers } from "./gmail";
import { grocyRenderers } from "./grocy";
import { tanaRenderers } from "./tana";
import { sandboxRenderers } from "./sandbox";
import { sshRenderers } from "./ssh/index";

type ActionIdentity = ActionRequestView["action"];

// Maps avoid prototype-key lookups for group and Action names.
const ACTION_RENDERERS: ReadonlyMap<string, ReadonlyMap<string, ActionPresentation>> = new Map([
  ["kubernetes_admin", kubernetesAdminRenderers],
  ["github", githubRenderers],
  ["gmail", gmailRenderers],
  ["grocy_sf", grocyRenderers],
  ["tana", tanaRenderers],
  ["sandbox", sandboxRenderers],
  ["ssh", sshRenderers],
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

/** Whether this invocation gets card decision buttons; independent of the presentation chosen. */
export function actionOffersCardDecisionButtons(action: ActionIdentity, args: unknown): boolean {
  return presentation(action)?.offersCardDecisionButtons?.(args) ?? false;
}

/** Native executors store their result models directly, without an MCP envelope. */
export function renderNativeDetailsResult(action: ActionIdentity, value: unknown): ReactNode | null {
  const preview = presentation(action)?.details?.nativeResult;
  return preview ? renderResultPreview(preview, value) : null;
}
