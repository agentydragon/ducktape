// Every registered tool's one-line action description — "Gmail: Draft email", "kubectl: Delete
// Pod", "hostexec: Run on wyrm2 as root" — computed from the call's parsed arguments.
//
// **This module is deliberately React-free**, and must stay that way. It has two consumers with
// very different budgets: the approvals card's identity line (`../tool_action_line.tsx`), which
// is inside the SPA and could import anything; and the service worker (`../sw.ts`), which renders
// push notifications for pending calls and must not drag React, Mantine, and CodeMirror into a
// bundle the browser loads to show a notification. Keeping the descriptions here rather than
// beside each widget is what lets a notification say the same thing the console does — one source
// of truth for "what is this call?", not a second phrasing that drifts.
//
// Adding a tool: register its widget in `<server>/requests.tsx` (or `calls.tsx`) as before, and
// add its description here. A tool with no entry falls back to `serverId.toolName`.

import type { z } from "zod";

import { mcpToolSchema } from "../mcp_tool_schema.ts";
import { zResourcesDeleteArgs } from "./kubectl/schemas.ts";
import { zSetFieldOptionArgs } from "./tana/schemas.ts";
import {
  GMAIL_SERVER_ID,
  GOOGLE_CALENDAR_SERVER_ID,
  GROCY_SERVER_ID,
  HAKU_ROUTINE_SERVER_ID,
  HOSTEXEC_SERVER_ID,
  KUBECTL_SERVER_ID,
  TANA_RW_SERVER_ID,
} from "./server_ids.ts";

/** A registered tool's action description. `destructive` is a danger cue (irreversible deletes):
 * the card colors it red, and the notification says so in words. */
export type ToolAction = { text: string; destructive?: boolean };

type ActionEntry = { schema?: z.ZodTypeAny; describe: (args: never) => ToolAction };

/** Bind a tool's argument schema to a description computed from its parsed arguments. */
function fromArgs<S extends z.ZodTypeAny>(schema: S, describe: (args: z.infer<S>) => ToolAction): ActionEntry {
  return { schema, describe: describe as (args: never) => ToolAction };
}

/** A description that does not depend on the arguments — most tools. No schema, so nothing to
 * parse and nothing to keep in step with the widget's. */
function fixed(text: string, destructive?: boolean): ActionEntry {
  return { describe: () => (destructive ? { text, destructive } : { text }) };
}

/** "4 threads" / "1 item" — a count plus its naively pluralized noun. Duplicated from
 * `vocabulary.tsx` (which is React) rather than imported, to keep this module a leaf. */
function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

const ACTIONS: Record<string, Record<string, ActionEntry>> = {
  [GMAIL_SERVER_ID]: {
    drafts_create: fixed("Gmail: Draft email"),
    threads_modify_labels: fromArgs(mcpToolSchema(GMAIL_SERVER_ID, "threads_modify_labels"), (a) => ({
      text: `Gmail: Relabel ${plural(a.thread_ids.length, "thread")}`,
    })),
    threads_get: fixed("Gmail: Get thread"),
    threads_list: fixed("Gmail: Search threads"),
    messages_get: fixed("Gmail: Get message"),
  },
  [GOOGLE_CALENDAR_SERVER_ID]: {
    create_event: fromArgs(mcpToolSchema(GOOGLE_CALENDAR_SERVER_ID, "create_event"), (a) => ({
      text: a.recurrence?.length ? "Google Calendar: Create recurring event" : "Google Calendar: Create event",
    })),
    get_event: fixed("Google Calendar: Get event"),
    list_events: fixed("Google Calendar: List events"),
    list_event_instances: fixed("Google Calendar: List event instances"),
  },
  [GROCY_SERVER_ID]: {
    stock_add: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "stock_add"), (a) => ({
      text: `Grocy: Add ${plural(a.items.length, "item")} to stock`,
    })),
    stock_consume: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "stock_consume"), (a) => ({
      text: `Grocy: Remove ${plural(a.items.length, "item")} from stock`,
    })),
    stock_entry_edit: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "stock_entry_edit"), (a) => ({
      text: `Grocy: Edit ${a.items.length} stock ${a.items.length === 1 ? "entry" : "entries"}`,
    })),
    stock_get: fixed("Grocy: View stock"),
    products_list: fixed("Grocy: List products"),
    quantity_units_list: fixed("Grocy: List quantity units"),
    get_system_info: fixed("Grocy: View system information"),
    products_create: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "products_create"), (a) => ({
      text: `Grocy: Create ${plural(a.items.length, "product")}`,
    })),
    products_edit: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "products_edit"), (a) => ({
      text: `Grocy: Edit ${plural(a.items.length, "product")}`,
    })),
    shopping_list_get: fixed("Grocy: View shopping list"),
    shopping_list_items_add: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "shopping_list_items_add"), (a) => ({
      text: `Grocy: Add ${plural(a.items.length, "item")} to shopping list`,
    })),
    shopping_list_items_remove: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "shopping_list_items_remove"), (a) => ({
      text: `Grocy: Remove ${plural(a.item_ids.length, "shopping-list item")}`,
      destructive: true,
    })),
    shopping_list_item_edit: fixed("Grocy: Edit shopping list item"),
  },
  [HAKU_ROUTINE_SERVER_ID]: {
    launch_routine: fixed("Haku: Start a new run"),
  },
  [HOSTEXEC_SERVER_ID]: {
    bash: fromArgs(mcpToolSchema(HOSTEXEC_SERVER_ID, "bash"), (a) => ({
      text: `hostexec: Run on ${a.host} as ${a.run_as}`,
    })),
  },
  [KUBECTL_SERVER_ID]: {
    resources_create_or_update: fixed("kubectl: Apply resource"),
    resources_delete: fromArgs(zResourcesDeleteArgs, (a) => ({
      text: `kubectl: Delete ${a.kind}`,
      destructive: true,
    })),
    pods_delete: fixed("kubectl: Delete Pod", true),
    pods_log: fixed("kubectl: View Pod logs"),
  },
  [TANA_RW_SERVER_ID]: {
    import_tana_paste: fixed("Tana: Import content"),
    get_or_create_calendar_node: fixed("Tana: Get or create calendar node"),
    trash_node: fixed("Tana: Trash node", true),
    edit_node: fixed("Tana: Edit node"),
    move_node: fixed("Tana: Move node"),
    set_field_option: fromArgs(zSetFieldOptionArgs, (a) => ({
      text: `Tana: ${a.mode === "append" ? "Append" : "Set"} field option`,
    })),
  },
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
