// React presentation registry for Actions. Each Action can choose a human-facing label, a different
// widget for the collapsed and opened Actions pane, and full-detail arguments and results. The parallel,
// React-free notification registry lives in ../notifications because the service worker imports it.
// The group is the name the Action Service configures a backend under. Unknown identities or
// payloads fall back to the host's generic view.
import { Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";

import { type CallToolResult, toolContentRestatesValue, toolValue } from "../call_tool_result";
import type { ActionRequestView } from "../client";
import { ActionJson } from "../payload";
import { renderPreview } from "./entry";
import type { ActionOutcome, ActionPresentation } from "./presentation";
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

/** Interpret the tool's outcome using its own protocol; a generic receipt uses the execution state. */
export function renderActionOutcome(action: ActionIdentity, result: CallToolResult): ActionOutcome | undefined {
  if (result.isError) return undefined;
  return presentation(action)?.outcome?.(toolValue(result));
}

/** An Action's human-facing name; `undefined` uses the host label and `null` suppresses the label. */
export function renderActionLabel(action: ActionIdentity, args: unknown): ReactNode | null | undefined {
  const actionPresentation = presentation(action);
  if (!actionPresentation) return undefined;
  const { label, fallbackLabel } = actionPresentation;
  if (label === undefined) return fallbackLabel;
  if (label === null) return null;
  const rendered = renderPreview(label, args);
  return rendered === undefined ? fallbackLabel : rendered;
}

/** An Action's collapsed-pane summary; `undefined` uses the host fallback and `null` suppresses it. */
export function renderPaneCollapsed(action: ActionIdentity, args: unknown): ReactNode | null | undefined {
  const preview = presentation(action)?.pane?.collapsed;
  return preview === undefined || preview === null ? preview : renderPreview(preview, args);
}

/** An Action's opened-pane view; `undefined` uses the generic argument view and `null` suppresses it. */
export function renderPaneOpened(action: ActionIdentity, args: unknown): ReactNode | null | undefined {
  const preview = presentation(action)?.pane?.opened;
  if (preview === undefined || preview === null) return preview;
  const rendered = renderPreview(preview, args);
  return rendered === undefined ? recordedDataFallback(args) : rendered;
}

/** Whether the caller's title repeats the Action label shown in the pane heading. */
export function shouldRenderPaneRequestTitle(action: ActionIdentity, args: unknown, title: string): boolean {
  const customCheck = presentation(action)?.pane?.requestTitleIsRedundant;
  return !(customCheck?.(title, args) ?? false);
}

/** The pretty argument view on the full details page; Raw remains the host's shared exact-JSON view. */
export function renderDetailsArguments(action: ActionIdentity, args: unknown): ReactNode | null | undefined {
  const preview = presentation(action)?.details?.arguments;
  if (preview === undefined || preview === null) return preview;
  const rendered = renderPreview(preview, args);
  return rendered === undefined ? recordedDataFallback(args) : rendered;
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
): RenderedActionCall | null | undefined {
  const preview = presentation(action)?.call;
  if (!preview) return undefined;
  const parsedArgs = preview.argumentSchema.safeParse(args);
  if (!parsedArgs.success) return undefined;

  if (result === undefined || result.isError) {
    return { content: preview.render(parsedArgs.data as never, undefined), includesResult: false };
  }

  const value = toolValue(result);
  if (value === undefined || !toolContentRestatesValue(result, value)) return undefined;
  const parsedResult = preview.resultSchema.safeParse(value);
  if (!parsedResult.success) return undefined;
  return {
    content: preview.render(parsedArgs.data as never, parsedResult.data as never),
    includesResult: true,
  };
}

/** An MCP result's action-specific view; `undefined` asks the host to render the whole MCP envelope. */
export function renderDetailsResult(action: ActionIdentity, result: CallToolResult): ReactNode | null | undefined {
  if (result.isError) return undefined;
  const preview = presentation(action)?.details?.result;
  if (preview === undefined || preview === null) return preview;
  const value = toolValue(result);
  if (value === undefined || !toolContentRestatesValue(result, value)) return undefined;
  return renderResultPreview(preview, value);
}

/** Whether this invocation gets card decision buttons; independent of the presentation chosen. */
export function actionOffersCardDecisionButtons(action: ActionIdentity, args: unknown): boolean {
  return presentation(action)?.offersCardDecisionButtons?.(args) ?? false;
}

/** Native executors store their result models directly, without an MCP envelope. */
export function renderNativeDetailsResult(action: ActionIdentity, value: unknown): ReactNode | null | undefined {
  const preview = presentation(action)?.details?.nativeResult;
  return preview === undefined || preview === null ? preview : renderResultPreview(preview, value);
}

/** A strict custom schema that cannot decode stored data gets an explicit, lossless fallback. */
function recordedDataFallback(value: unknown): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="xs" c="dimmed">
        Details shown as recorded data
      </Text>
      <ActionJson value={value} />
    </Stack>
  );
}
