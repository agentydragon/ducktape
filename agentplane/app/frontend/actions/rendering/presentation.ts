import type { ReactNode } from "react";

import type { ArgumentsPreview, CallPreview } from "./entry";
import type { ResultPreview } from "./result_entry";

export interface ActionOutcome {
  label: string;
  failed: boolean;
}

export interface ActionPresentation {
  /** The Action's interpretation of its returned value, separate from successful dispatch. */
  outcome?: (value: unknown) => ActionOutcome | undefined;
  /** A stable human-facing operation name used when `label` cannot decode the arguments. */
  fallbackLabel?: ReactNode;
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
    nativeResult?: ResultPreview | null;
  };
  /** Card decision buttons are a capability of their own: a custom view does not grant them. The Action decides per
      invocation, from the complete arguments, whether what the compact card shows is enough to decide on. */
  offersCardDecisionButtons?: (args: unknown) => boolean;
}
