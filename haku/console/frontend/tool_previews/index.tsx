// Registry mapping each MCP server id to its per-tool preview entries. Each per-server module
// (gmail.tsx, google_calendar.tsx, …) owns one server's widgets and exports its serverId plus a
// `{ toolName -> {schema, render} }` map; adding a server is "write a new module + one registry
// entry here". `toolPreview` dispatches by serverId, safeParses the arguments against the tool's
// schema once, and hands the widget already-typed data — no module repeats the parse, and the
// file never grows a hand-maintained `??` chain. `variant` picks the compact vs detailed rendering.

import type { ReactNode } from "react";

import { describeAction, renderPreview, type ToolAction, type ToolPreview } from "./entry.tsx";
import { GMAIL_SERVER_ID, gmailPreviews } from "./gmail.tsx";
import { GOOGLE_CALENDAR_SERVER_ID, googleCalendarPreviews } from "./google_calendar.tsx";
import { GROCY_SERVER_ID, grocyPreviews } from "./grocy.tsx";
import { HAKU_ROUTINE_SERVER_ID, hakuRoutinePreviews } from "./haku_routine.tsx";
import { KUBECTL_SERVER_ID, kubectlPreviews } from "./kubectl.tsx";
import type { PreviewVariant } from "./variant.tsx";

const REGISTRY: Record<string, Record<string, ToolPreview>> = {
  [GMAIL_SERVER_ID]: gmailPreviews,
  [GOOGLE_CALENDAR_SERVER_ID]: googleCalendarPreviews,
  [GROCY_SERVER_ID]: grocyPreviews,
  [HAKU_ROUTINE_SERVER_ID]: hakuRoutinePreviews,
  [KUBECTL_SERVER_ID]: kubectlPreviews,
};

export function toolPreview(
  serverId: string,
  toolName: string,
  args: Record<string, unknown>,
  variant: PreviewVariant
): ReactNode | null {
  const preview = REGISTRY[serverId]?.[toolName];
  return preview ? renderPreview(preview, args, variant) : null;
}

/** A registered tool's action description for the card's identity line, or `null` when no widget
 * matches (the caller falls back to `serverId.toolName`). */
export function toolActionDescription(
  serverId: string,
  toolName: string,
  args: Record<string, unknown>
): ToolAction | null {
  const preview = REGISTRY[serverId]?.[toolName];
  return preview ? describeAction(preview, args) : null;
}
