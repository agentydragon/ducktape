// Registries mapping each MCP server id to its per-tool rendering entries. Most tools split
// pending/finished rendering across two independent widgets: `<server>/requests.tsx` exports the
// server id plus a `{ toolName -> {schema, render} }` map for the call's *arguments*, and
// `<server>/responses.tsx` the same for its *result* (a request-only server has no responses
// module). A tool whose pending and finished states are naturally one evolving view — a creation
// tool whose result mostly restates its arguments — instead registers once in `<server>/calls.tsx`'s
// combined map (see call_entry.tsx). A tool lives in exactly one of the two systems, never both.
// Adding a server is "write a new directory + registry entries here".
// `toolPreview`/`toolResultPreview`/`toolCallPreview` dispatch by serverId, safeParse the payload
// against the tool's schema(s) once, and hand the widget already-typed data. `variant` picks the
// compact vs detailed rendering.

import type { ReactNode } from "react";
import type { z } from "zod";

import { renderCallPreview, type ToolCallPreview } from "./call_entry";
import { renderPreview, type ToolPreview } from "./entry";
import { gmailCallPreviews } from "./gmail/calls";
import { gmailPreviews } from "./gmail/requests";
import { gmailResultPreviews } from "./gmail/responses";
import { googleCalendarCallPreviews } from "./google_calendar/calls";
import { googleCalendarPreviews } from "./google_calendar/requests";
import { googleCalendarResultPreviews } from "./google_calendar/responses";
import { grocyPreviews } from "./grocy/requests";
import { grocyResultPreviews } from "./grocy/responses";
import { hakuRoutinePreviews } from "./haku_routine/requests";
import { hostexecPreviews } from "./hostexec/requests";
import { hostexecResultPreviews } from "./hostexec/responses";
import { grantsPreviews } from "./grants/requests";
import { grantsResultPreviews } from "./grants/responses";
import { kubectlPreviews } from "./kubectl/requests";
import { renderResultPreview, type ToolResultPreview } from "./result_entry";
import {
  GMAIL_SERVER_ID,
  GOOGLE_CALENDAR_SERVER_ID,
  GRANTS_SERVER_ID,
  GROCY_SERVER_ID,
  HAKU_ROUTINE_SERVER_ID,
  HOSTEXEC_SERVER_ID,
  KUBECTL_SERVER_ID,
  TANA_RW_SERVER_ID,
} from "./server_ids";
import { tanaPreviews } from "./tana/requests";
import type { PreviewVariant } from "./vocabulary";

// isolatedDeclarations cannot infer a computed-key object literal's type, so the shape is spelled
// out once here; REGISTRY's explicit annotation makes `typeof REGISTRY` below resolve to exactly
// this (still-literal, per-server) type instead of forcing a widened one.
type PreviewRegistryShape = {
  [GMAIL_SERVER_ID]: typeof gmailPreviews;
  [GOOGLE_CALENDAR_SERVER_ID]: typeof googleCalendarPreviews;
  [GROCY_SERVER_ID]: typeof grocyPreviews;
  [HAKU_ROUTINE_SERVER_ID]: typeof hakuRoutinePreviews;
  [HOSTEXEC_SERVER_ID]: typeof hostexecPreviews;
  [KUBECTL_SERVER_ID]: typeof kubectlPreviews;
  [GRANTS_SERVER_ID]: typeof grantsPreviews;
  [TANA_RW_SERVER_ID]: typeof tanaPreviews;
};

const REGISTRY: PreviewRegistryShape = {
  [GMAIL_SERVER_ID]: gmailPreviews,
  [GOOGLE_CALENDAR_SERVER_ID]: googleCalendarPreviews,
  [GROCY_SERVER_ID]: grocyPreviews,
  [HAKU_ROUTINE_SERVER_ID]: hakuRoutinePreviews,
  [HOSTEXEC_SERVER_ID]: hostexecPreviews,
  [KUBECTL_SERVER_ID]: kubectlPreviews,
  [GRANTS_SERVER_ID]: grantsPreviews,
  [TANA_RW_SERVER_ID]: tanaPreviews,
} satisfies Record<string, Record<string, ToolPreview>>;

type PreviewRegistry = typeof REGISTRY;
const RUNTIME_REGISTRY: Record<string, Record<string, ToolPreview>> = REGISTRY;

// `as const` (like REGISTRY) so RegisteredResultPayload below can resolve each tool's literal
// result schema; RUNTIME_RESULT_REGISTRY is the widened twin toolResultPreview indexes by string.
type ResultRegistryShape = {
  [GMAIL_SERVER_ID]: typeof gmailResultPreviews;
  [GOOGLE_CALENDAR_SERVER_ID]: typeof googleCalendarResultPreviews;
  [GROCY_SERVER_ID]: typeof grocyResultPreviews;
  [HOSTEXEC_SERVER_ID]: typeof hostexecResultPreviews;
  [GRANTS_SERVER_ID]: typeof grantsResultPreviews;
};

const RESULT_REGISTRY: ResultRegistryShape = {
  [GMAIL_SERVER_ID]: gmailResultPreviews,
  [GOOGLE_CALENDAR_SERVER_ID]: googleCalendarResultPreviews,
  [GROCY_SERVER_ID]: grocyResultPreviews,
  [HOSTEXEC_SERVER_ID]: hostexecResultPreviews,
  [GRANTS_SERVER_ID]: grantsResultPreviews,
} satisfies Record<string, Record<string, ToolResultPreview>>;
type ResultRegistry = typeof RESULT_REGISTRY;
const RUNTIME_RESULT_REGISTRY: Record<string, Record<string, ToolResultPreview>> = RESULT_REGISTRY;

// Combined pending/finished widgets — see call_entry.tsx.
type CallRegistryShape = {
  [GMAIL_SERVER_ID]: typeof gmailCallPreviews;
  [GOOGLE_CALENDAR_SERVER_ID]: typeof googleCalendarCallPreviews;
};

const CALL_REGISTRY: CallRegistryShape = {
  [GMAIL_SERVER_ID]: gmailCallPreviews,
  [GOOGLE_CALENDAR_SERVER_ID]: googleCalendarCallPreviews,
} satisfies Record<string, Record<string, ToolCallPreview>>;
type CallRegistry = typeof CALL_REGISTRY;
const RUNTIME_CALL_REGISTRY: Record<string, Record<string, ToolCallPreview>> = CALL_REGISTRY;

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

// A fixture for a tool registered on the args/result side (REGISTRY, optionally RESULT_REGISTRY).
type RegisteredArgsResultFixture = {
  [ServerId in keyof PreviewRegistry]: {
    [ToolName in keyof PreviewRegistry[ServerId]]: PreviewRegistry[ServerId][ToolName] extends ToolPreview<infer Schema>
      ? {
          agentDisplayNames?: Record<string, string>;
          serverId: ServerId;
          toolName: ToolName;
          args: z.output<Schema>;
          result?: RegisteredResultPayload<ServerId, ToolName>;
        }
      : never;
  }[keyof PreviewRegistry[ServerId]];
}[keyof PreviewRegistry];

// A fixture for a tool registered on the combined side (CALL_REGISTRY) — both schemas come off
// the one entry instead of a separate result-registry lookup.
type RegisteredCallFixture = {
  [ServerId in keyof CallRegistry]: {
    [ToolName in keyof CallRegistry[ServerId]]: CallRegistry[ServerId][ToolName] extends ToolCallPreview<
      infer ArgsSchema,
      infer ResultSchema
    >
      ? {
          agentDisplayNames?: Record<string, string>;
          serverId: ServerId;
          toolName: ToolName;
          args: z.output<ArgsSchema>;
          result?: z.output<ResultSchema>;
        }
      : never;
  }[keyof CallRegistry[ServerId]];
}[keyof CallRegistry];

/** A fixture whose server, tool, and arguments (plus optional result) are tied to one registered
 * preview schema — either the args/result registry pair, or (for a tool with one combined
 * pending/finished widget) the call registry. In-process schemas originate in FastMCP's
 * tools/list catalog; remote-server previews retain their hand-authored Zod contract. `satisfies
 * RegisteredToolPreviewFixture` therefore makes fixture drift a TypeScript error without widening
 * the fixture's literal values. `result?` is the tool's raw return value, typed by its result
 * schema where one is registered (else `never`, so the fixture cannot carry a result); the
 * screenshot harness wraps it into the stored CallToolResult envelope. `z.output` is intentional:
 * the generated adapter pins `ZodType<GeneratedArguments>` as its output type, while Zod 4 leaves
 * that generic type's input as `unknown`. */
export type RegisteredToolPreviewFixture = RegisteredArgsResultFixture | RegisteredCallFixture;

export function toolPreview(
  serverId: string,
  toolName: string,
  args: Record<string, unknown>,
  variant: PreviewVariant
): ReactNode | null {
  const preview = RUNTIME_REGISTRY[serverId]?.[toolName];
  return preview ? renderPreview(preview, args, variant) : null;
}

/** A registered tool's action description for the card's identity line, or `null` when no widget
 * matches (the caller falls back to `serverId.toolName`). */

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

/** The registered combined widget for one tool's pending/finished states, or `null` when no
 * combined preview matches (the caller falls back to the separate arguments/result fields). */
export function toolCallPreview(
  serverId: string,
  toolName: string,
  args: Record<string, unknown>,
  rawResult: unknown,
  variant: PreviewVariant
): ReactNode | null {
  const preview = RUNTIME_CALL_REGISTRY[serverId]?.[toolName];
  return preview ? renderCallPreview(preview, args, rawResult, variant) : null;
}
