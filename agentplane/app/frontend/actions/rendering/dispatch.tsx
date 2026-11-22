import { Stack, Text } from "@mantine/core";
import { createContext, useContext, type Context, type JSX, type ReactNode } from "react";

import { type CallToolResult, toolContentRestatesValue, toolValue } from "../call_tool_result";
import type { ActionIdentity } from "../../../../action_service/api/contracts/action_service_pb";
import { JsonBlock } from "../../ui/text_block";
import { CompactPaneContent, renderPreview } from "./entry";
import type { ActionOutcome, ActionPresentation, CardDecisionProps } from "./presentation";
import { renderResultPreview, type ResultPreviewContext } from "./result_entry";
export interface RenderedActionCall {
  content: ReactNode;
  /** Whether this view includes a successfully parsed result and can replace the separate result block. */
  includesResult: boolean;
}

export type ActionRegistry = ReadonlyMap<string, ReadonlyMap<string, ActionPresentation>>;

export class ActionRendering {
  constructor(private readonly registry: ActionRegistry) {}

  private presentation(action: ActionIdentity): ActionPresentation | undefined {
    return this.registry.get(action.group)?.get(action.name);
  }

  /** Interpret the tool's outcome using its own protocol; a generic receipt uses the execution state. */
  renderActionOutcome(action: ActionIdentity, result: CallToolResult): ActionOutcome | undefined {
    if (result.isError) return undefined;
    return this.presentation(action)?.outcome?.(toolValue(result));
  }

  /** An Action's human-facing name; `undefined` uses the host label and `null` suppresses the label. */
  renderActionLabel(action: ActionIdentity, args: unknown, requestId?: string): ReactNode | null | undefined {
    const actionPresentation = this.presentation(action);
    if (!actionPresentation) return undefined;
    const { label, fallbackLabel } = actionPresentation;
    if (label === undefined) return fallbackLabel;
    if (label === null) return null;
    const rendered = renderPreview(label, args, requestId);
    return rendered === undefined ? fallbackLabel : rendered;
  }

  /** An Action's collapsed-pane summary; `undefined` uses the host fallback and `null` suppresses it. */
  renderPaneCollapsed(action: ActionIdentity, args: unknown, requestId?: string): ReactNode | null | undefined {
    const preview = this.presentation(action)?.pane?.collapsed;
    const content = preview === undefined || preview === null ? preview : renderPreview(preview, args, requestId);
    return content == null ? content : <CompactPaneContent>{content}</CompactPaneContent>;
  }

  /** An Action's opened-pane view; `undefined` uses the generic argument view and `null` suppresses it. */
  renderPaneOpened(action: ActionIdentity, args: unknown, requestId?: string): ReactNode | null | undefined {
    const preview = this.presentation(action)?.pane?.opened;
    if (preview === undefined || preview === null) return preview;
    const rendered = renderPreview(preview, args, requestId);
    return rendered === undefined ? recordedDataFallback(args) : rendered;
  }

  /** Show the caller's own title unless it repeats the operation heading. */
  shouldRenderRequestTitle(action: ActionIdentity, args: unknown, title: string, label: ReactNode): boolean {
    const heading = label === undefined ? `${action.group} / ${action.name}` : label;
    const customCheck = this.presentation(action)?.requestTitleIsRedundant;
    return title.trim() !== heading && !(customCheck?.(title, args) ?? false);
  }

  /** The pretty argument view on the full details page; Raw remains the host's shared exact-JSON view. */
  renderDetailsArguments(
    action: ActionIdentity,
    args: unknown,
    requestId?: string,
    pending = false
  ): ReactNode | null | undefined {
    const details = this.presentation(action)?.details;
    const preview = details?.arguments;
    if (preview === undefined || preview === null) return preview;
    const rendered = renderPreview(preview, args, details?.pendingPreviewOnly && !pending ? undefined : requestId);
    return rendered === undefined ? recordedDataFallback(args) : rendered;
  }

  /** A combined per-Action request/result view, or `null` when its action-local schemas do not match. */
  renderActionCall(
    action: ActionIdentity,
    args: unknown,
    result?: CallToolResult,
    requestId?: string
  ): RenderedActionCall | null | undefined {
    const preview = this.presentation(action)?.call;
    if (!preview) return undefined;
    const parsedArgs = preview.argumentSchema.safeParse(args);
    if (!parsedArgs.success) return undefined;

    if (result === undefined || result.isError) {
      return { content: preview.render(parsedArgs.data as never, undefined, requestId), includesResult: false };
    }

    const value = toolValue(result);
    if (value === undefined || !toolContentRestatesValue(result, value)) return undefined;
    const parsedResult = preview.resultSchema.safeParse(value);
    if (!parsedResult.success) return undefined;
    return {
      content: preview.render(parsedArgs.data as never, parsedResult.data as never, requestId),
      includesResult: true,
    };
  }

  /** An MCP result's action-specific view; `undefined` asks the host to render the whole MCP envelope. */
  renderDetailsResult(
    action: ActionIdentity,
    result: CallToolResult,
    context?: ResultPreviewContext
  ): ReactNode | null | undefined {
    const details = this.presentation(action)?.details;
    if (details?.result === null) return null;
    if (result.isError) return undefined;
    if (details?.envelopeResult) {
      const rendered = renderResultPreview(details.envelopeResult, result, context);
      if (rendered !== undefined) return rendered;
    }
    const value = toolValue(result);
    if (result.content.length === 0 && value == null && details?.emptyResult !== undefined) {
      return details.emptyResult;
    }
    const preview = details?.result;
    if (preview === undefined) return undefined;
    if (value === undefined || !toolContentRestatesValue(result, value)) return undefined;
    return renderResultPreview(preview, value, context);
  }

  /** Whether this invocation gets card decision buttons; independent of the presentation chosen. */
  actionOffersCardDecisionButtons(action: ActionIdentity, args: unknown): boolean {
    return this.presentation(action)?.offersCardDecisionButtons?.(args) ?? false;
  }

  renderLiveCardDecision(action: ActionIdentity, props: CardDecisionProps, expanded: boolean): ReactNode {
    const decision = this.presentation(action)?.liveCardDecision;
    if (!decision || (!expanded && !decision.collapsed)) return props.fallback;
    const Decision = decision.component;
    return <Decision {...props} />;
  }

  /** Whether this invocation's compact card opens; unknown Actions open to their recorded arguments. */
  actionCompactCardExpands(action: ActionIdentity, args: unknown): boolean {
    return this.presentation(action)?.compactCardExpands?.(args) ?? true;
  }

  /** Native executors store their result models directly, without an MCP envelope. */
  renderNativeDetailsResult(action: ActionIdentity, value: unknown): ReactNode | null | undefined {
    const preview = this.presentation(action)?.details?.nativeResult;
    return preview === undefined || preview === null ? preview : renderResultPreview(preview, value);
  }
}

export const ActionRenderingContext: Context<ActionRendering | null> = createContext<ActionRendering | null>(null);

export function useActionRendering(): ActionRendering {
  const rendering = useContext(ActionRenderingContext);
  if (!rendering) throw new Error("Action rendering requires a registry provider");
  return rendering;
}

/** A strict custom schema that cannot decode stored data gets an explicit, lossless fallback. */
function recordedDataFallback(value: unknown): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="xs" c="dimmed">
        Details shown as recorded data
      </Text>
      <JsonBlock value={value} />
    </Stack>
  );
}
