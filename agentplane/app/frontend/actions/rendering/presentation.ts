import type { ComponentType, ReactNode } from "react";
import type { z } from "zod";

import type { ArgumentsPreview, CallPreview } from "./entry";
import type { ResultPreview } from "./result_entry";

export interface ActionOutcome {
  label: string;
  failed: boolean;
}

export interface ExpandedCardDecisionProps {
  args: unknown;
  requestId: string;
  buttons: ReactNode;
  fallback: ReactNode;
}

export interface ActionPresentation {
  /** The Action's interpretation of its returned value, separate from successful dispatch. */
  outcome?: (value: unknown) => ActionOutcome | undefined;
  /** A stable human-facing operation name used when `label` cannot decode the arguments. */
  fallbackLabel?: ReactNode;
  /** Omit a slot for the generic fallback, set null to suppress it, or provide its own React DOM. */
  label?: ArgumentsPreview | null;
  /** Action-specific equality when the label is built from arguments and rendered as JSX. */
  requestTitleIsRedundant?: (title: string, args: unknown) => boolean;
  /** A custom view that keeps an Action's arguments and returned value together. */
  call?: CallPreview;
  pane?: {
    collapsed?: ArgumentsPreview | null;
    opened?: ArgumentsPreview | null;
  };
  details?: {
    arguments?: ArgumentsPreview | null;
    /** Live argument enrichment is available while the operator reviews the pending call. */
    pendingPreviewOnly?: boolean;
    result?: ResultPreview | null;
    /** A successful MCP call whose provider returns no content. */
    emptyResult?: ReactNode;
    /** Decode the full MCP envelope when the provider returns different text and structured projections. */
    envelopeResult?: ResultPreview;
    nativeResult?: ResultPreview | null;
  };
  /** A custom view does not grant decision buttons. The Action checks whether the current card shows the complete call. */
  offersCardDecisionButtons?: (args: unknown, expanded: boolean) => boolean;
  /** When the opened preview depends on live data, use that same lookup to gate the decisions. */
  expandedCardDecision?: ComponentType<ExpandedCardDecisionProps>;
  /** Whether this invocation's compact card has an expanded version; omitted, it does. Return false only when the
      closed card already shows everything the opened one would, so arguments the schemas reject keep it for their
      recorded-data view. */
  compactCardExpands?: (args: unknown) => boolean;
}

/** `compactCardExpands` for an Action whose closed card shows every argument `schema` accepts: only arguments it
    rejects open, to their recorded data. */
export function expandsForRejectedArguments(schema: z.ZodType): (args: unknown) => boolean {
  return (args) => !schema.safeParse(args).success;
}
