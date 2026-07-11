// Registry mapping each MCP server id to its per-tool preview renderer. Each per-server
// module (gmail.tsx, google_calendar.tsx, …) owns one server's widgets and exports its
// serverId plus a (toolName, args, variant) => ReactNode|null renderer; adding a server is
// "write a new module + one registry entry here". `toolPreview` dispatches by serverId, so the
// file never grows a hand-maintained `??` chain. `variant` picks the compact vs detailed
// rendering.

import type { ReactNode } from "react";

import { GMAIL_SERVER_ID, gmailToolPreview } from "./gmail.tsx";
import { GOOGLE_CALENDAR_SERVER_ID, googleCalendarToolPreview } from "./google_calendar.tsx";
import { GROCY_SERVER_ID, grocyToolPreview } from "./grocy.tsx";
import { KUBECTL_SERVER_ID, kubectlToolPreview } from "./kubectl.tsx";
import type { PreviewVariant } from "./variant.tsx";

type ToolPreviewRenderer = (
  toolName: string,
  args: Record<string, unknown>,
  variant: PreviewVariant
) => ReactNode | null;

const RENDERERS: Record<string, ToolPreviewRenderer> = {
  [GMAIL_SERVER_ID]: gmailToolPreview,
  [GOOGLE_CALENDAR_SERVER_ID]: googleCalendarToolPreview,
  [GROCY_SERVER_ID]: grocyToolPreview,
  [KUBECTL_SERVER_ID]: kubectlToolPreview,
};

export function toolPreview(
  serverId: string,
  toolName: string,
  args: Record<string, unknown>,
  variant: PreviewVariant
): ReactNode | null {
  return RENDERERS[serverId]?.(toolName, args, variant) ?? null;
}
