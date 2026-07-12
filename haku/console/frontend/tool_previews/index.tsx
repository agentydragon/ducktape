// Registry mapping each MCP server id to its per-tool preview entries. Each per-server module
// (gmail.tsx, google_calendar.tsx, …) owns one server's widgets and exports its serverId plus a
// `{ toolName -> {schema, render} }` map; adding a server is "write a new module + one registry
// entry here". `toolPreview` dispatches by serverId, safeParses the arguments against the tool's
// schema once, and hands the widget already-typed data — no module repeats the parse, and the
// file never grows a hand-maintained `??` chain. `variant` picks the compact vs detailed rendering.

import type { ReactNode } from "react";
import type { z } from "zod";

import { describeAction, renderPreview, type ToolAction, type ToolPreview } from "./entry.tsx";
import { GMAIL_SERVER_ID, gmailPreviews } from "./gmail.tsx";
import { GOOGLE_CALENDAR_SERVER_ID, googleCalendarPreviews } from "./google_calendar.tsx";
import { GROCY_SERVER_ID, grocyPreviews } from "./grocy.tsx";
import { HAKU_ROUTINE_SERVER_ID, hakuRoutinePreviews } from "./haku_routine.tsx";
import { KUBECTL_SERVER_ID, kubectlPreviews } from "./kubectl.tsx";
import { TANA_RW_SERVER_ID, tanaPreviews } from "./tana.tsx";
import type { PreviewVariant } from "./variant.tsx";

const REGISTRY = {
  [GMAIL_SERVER_ID]: gmailPreviews,
  [GOOGLE_CALENDAR_SERVER_ID]: googleCalendarPreviews,
  [GROCY_SERVER_ID]: grocyPreviews,
  [HAKU_ROUTINE_SERVER_ID]: hakuRoutinePreviews,
  [KUBECTL_SERVER_ID]: kubectlPreviews,
  [TANA_RW_SERVER_ID]: tanaPreviews,
} as const satisfies Record<string, Record<string, ToolPreview>>;

type PreviewRegistry = typeof REGISTRY;
const RUNTIME_REGISTRY: Record<string, Record<string, ToolPreview>> = REGISTRY;

/** A fixture whose server, tool, and arguments are tied to one registered preview schema.
 * In-process schemas originate in FastMCP's tools/list catalog; remote-server previews retain
 * their hand-authored Zod contract. `satisfies RegisteredToolPreviewFixture` therefore makes
 * fixture drift a TypeScript error without widening the fixture's literal values. `z.output`
 * is intentional: the generated adapter pins `ZodType<GeneratedArguments>` as its output type,
 * while Zod 4 leaves that generic type's input as `unknown`. */
export type RegisteredToolPreviewFixture = {
  [ServerId in keyof PreviewRegistry]: {
    [ToolName in keyof PreviewRegistry[ServerId]]: PreviewRegistry[ServerId][ToolName] extends ToolPreview<infer Schema>
      ? { serverId: ServerId; toolName: ToolName; args: z.output<Schema> }
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

/** A registered tool's action description for the card's identity line, or `null` when no widget
 * matches (the caller falls back to `serverId.toolName`). */
export function toolActionDescription(
  serverId: string,
  toolName: string,
  args: Record<string, unknown>
): ToolAction | null {
  const preview = RUNTIME_REGISTRY[serverId]?.[toolName];
  return preview ? describeAction(preview, args) : null;
}
