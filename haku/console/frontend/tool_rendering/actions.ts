// Registry mapping each MCP server id to its per-tool action descriptions — the one-line summary
// the approvals card's identity line shows ("Gmail: Draft email", "kubectl: Delete Pod") and the
// title a push notification is rendered with.
//
// Composes the per-server maps in `<server>/actions.ts`, exactly as `index.tsx` composes the
// per-server widget registries: adding a server is "write a new directory + a registry entry
// here". A tool with no entry falls back to `serverId.toolName`.
//
// **This side of the split is React-free**, and must stay that way. The card could import
// anything, but the service worker (`../sw.ts`) also reads this registry to title notifications
// for pending calls, and must not drag React, Mantine, and CodeMirror into a bundle the browser
// loads to show a notification. Sharing one registry is also what stops the two surfaces from
// drifting into different phrasings for the same call.

import type { ActionEntry, ToolAction } from "./action_entry.ts";
import { gmailActions } from "./gmail/actions.ts";
import { googleCalendarActions } from "./google_calendar/actions.ts";
import { grocyActions } from "./grocy/actions.ts";
import { hakuRoutineActions } from "./haku_routine/actions.ts";
import { hostexecActions } from "./hostexec/actions.ts";
import { kubectlActions } from "./kubectl/actions.ts";
import {
  GMAIL_SERVER_ID,
  GOOGLE_CALENDAR_SERVER_ID,
  GROCY_SERVER_ID,
  HAKU_ROUTINE_SERVER_ID,
  HOSTEXEC_SERVER_ID,
  KUBECTL_SERVER_ID,
  TANA_RW_SERVER_ID,
} from "./server_ids.ts";
import { tanaActions } from "./tana/actions.ts";

const ACTIONS: Record<string, Record<string, ActionEntry>> = {
  [GMAIL_SERVER_ID]: gmailActions,
  [GOOGLE_CALENDAR_SERVER_ID]: googleCalendarActions,
  [GROCY_SERVER_ID]: grocyActions,
  [HAKU_ROUTINE_SERVER_ID]: hakuRoutineActions,
  [HOSTEXEC_SERVER_ID]: hostexecActions,
  [KUBECTL_SERVER_ID]: kubectlActions,
  [TANA_RW_SERVER_ID]: tanaActions,
};

/** The tool's action description, or `null` when it has no entry or its args don't parse — a
 * pending call's arguments aren't validated until execution, so they may well be malformed. The
 * caller then falls back to `serverId.toolName`. */
export function toolActionDescription(
  serverId: string,
  toolName: string,
  args: Record<string, unknown>
): ToolAction | null {
  const entry = ACTIONS[serverId]?.[toolName];
  if (!entry) return null;
  if (!entry.schema) return entry.describe(undefined as never);
  const parsed = entry.schema.safeParse(args);
  return parsed.success ? entry.describe(parsed.data as never) : null;
}
