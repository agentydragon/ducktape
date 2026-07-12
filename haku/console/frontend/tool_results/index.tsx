// Registry mapping each MCP server id to its per-tool *result* preview entries — the
// result-side mirror of tool_previews/index.tsx. Each per-server module (gmail.tsx,
// google_calendar.tsx, …) owns one server's result widgets and exports a
// `{ toolName -> {schema, render} }` map; adding a server is "write a new module + one registry
// entry here". `toolResultPreview` dispatches by serverId, safeParses the unwrapped result
// payload against the tool's schema once, and hands the widget already-typed data. `variant`
// picks the compact vs detailed rendering. The server ids come from the tool_previews modules,
// which define them.

import type { ReactNode } from "react";

import { GMAIL_SERVER_ID } from "../tool_previews/gmail.tsx";
import { GOOGLE_CALENDAR_SERVER_ID } from "../tool_previews/google_calendar.tsx";
import { GROCY_SERVER_ID } from "../tool_previews/grocy.tsx";
import type { PreviewVariant } from "../tool_previews/variant.tsx";
import { renderResultPreview, type ToolResultPreview } from "./entry.tsx";
import { gmailResultPreviews } from "./gmail.tsx";
import { googleCalendarResultPreviews } from "./google_calendar.tsx";
import { grocyResultPreviews } from "./grocy.tsx";

const REGISTRY: Record<string, Record<string, ToolResultPreview>> = {
  [GMAIL_SERVER_ID]: gmailResultPreviews,
  [GOOGLE_CALENDAR_SERVER_ID]: googleCalendarResultPreviews,
  [GROCY_SERVER_ID]: grocyResultPreviews,
};

/** The registered widget for one tool's unwrapped result payload, or `null` when no widget
 * matches (the caller falls back to the raw-JSON result field). */
export function toolResultPreview(
  serverId: string,
  toolName: string,
  payload: unknown,
  variant: PreviewVariant
): ReactNode | null {
  const preview = REGISTRY[serverId]?.[toolName];
  return preview ? renderResultPreview(preview, payload, variant) : null;
}
