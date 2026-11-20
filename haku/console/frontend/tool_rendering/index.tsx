// Preview dispatch for the remaining Haku-only card widgets.

import type { ReactNode } from "react";
import type { z } from "zod";

import { renderPreview, type ToolPreview } from "./entry";
import { grocyPreviews } from "./grocy/requests";
import { grocyResultPreviews } from "./grocy/responses";
import { hakuRoutinePreviews } from "./haku_routine/requests";
import { renderResultPreview, type ToolResultPreview } from "./result_entry";
import { GROCY_SERVER_ID, HAKU_ROUTINE_SERVER_ID } from "./server_ids";
import type { PreviewVariant } from "./vocabulary";

// isolatedDeclarations cannot infer a computed-key object literal's type, so the shape is spelled
// out once here; REGISTRY's explicit annotation makes `typeof REGISTRY` below resolve to exactly
// this (still-literal, per-server) type instead of forcing a widened one.
type PreviewRegistryShape = {
  [GROCY_SERVER_ID]: typeof grocyPreviews;
  [HAKU_ROUTINE_SERVER_ID]: typeof hakuRoutinePreviews;
};

const REGISTRY: PreviewRegistryShape = {
  [GROCY_SERVER_ID]: grocyPreviews,
  [HAKU_ROUTINE_SERVER_ID]: hakuRoutinePreviews,
} satisfies Record<string, Record<string, ToolPreview>>;

type PreviewRegistry = typeof REGISTRY;
const RUNTIME_REGISTRY: Record<string, Record<string, ToolPreview>> = REGISTRY;

// `as const` (like REGISTRY) so RegisteredResultPayload below can resolve each tool's literal
// result schema; RUNTIME_RESULT_REGISTRY is the widened twin toolResultPreview indexes by string.
type ResultRegistryShape = {
  [GROCY_SERVER_ID]: typeof grocyResultPreviews;
};

const RESULT_REGISTRY: ResultRegistryShape = {
  [GROCY_SERVER_ID]: grocyResultPreviews,
} satisfies Record<string, Record<string, ToolResultPreview>>;
type ResultRegistry = typeof RESULT_REGISTRY;
const RUNTIME_RESULT_REGISTRY: Record<string, Record<string, ToolResultPreview>> = RESULT_REGISTRY;

/** The raw result payload a registered result widget parses for one (serverId, toolName); `never`
 * when the tool has no result widget, so a fixture for it cannot carry a result. Mirrors how the
 * argument side ties args to each tool's argument schema. */
type RegisteredResultPayload<
  ServerId extends string,
  ToolName extends PropertyKey,
> = ServerId extends keyof ResultRegistry
  ? ToolName extends keyof ResultRegistry[ServerId]
    ? ResultRegistry[ServerId][ToolName] extends ToolResultPreview<infer ResultSchema>
      ? z.output<ResultSchema>
      : never
    : never
  : never;

/** A fixture with arguments and any result typed against its preview schemas. */
export type RegisteredToolPreviewFixture = {
  [ServerId in keyof PreviewRegistry]: {
    [ToolName in keyof PreviewRegistry[ServerId]]: PreviewRegistry[ServerId][ToolName] extends ToolPreview<infer Schema>
      ? {
          serverId: ServerId;
          toolName: ToolName;
          args: z.output<Schema>;
          result?: RegisteredResultPayload<ServerId, ToolName>;
        }
      : never;
  }[keyof PreviewRegistry[ServerId]];
}[keyof PreviewRegistry];

export function toolPreview(
  serverId: string,
  toolName: string,
  args: Record<string, unknown>,
  variant: PreviewVariant
): ReactNode | null {
  const preview = RUNTIME_REGISTRY[serverId]?.[toolName];
  return preview ? renderPreview(preview, args, variant) : null;
}

/** The registered widget for one tool's unwrapped result payload, or `null` when no widget
 * matches (the caller falls back to the raw-JSON result field). */
export function toolResultPreview(
  serverId: string,
  toolName: string,
  payload: unknown,
  variant: PreviewVariant
): ReactNode | null {
  const preview = RUNTIME_RESULT_REGISTRY[serverId]?.[toolName];
  return preview ? renderResultPreview(preview, payload, variant) : null;
}
