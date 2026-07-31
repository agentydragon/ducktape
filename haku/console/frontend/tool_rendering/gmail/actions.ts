// Notification/card action descriptions for Gmail's tools — the one-line summary the
// approvals card shows and a push notification is titled with. Beside the widgets they
// describe, and React-free so `../../sw.ts` can bundle them (see ../action_entry.ts).

import { mcpToolSchema } from "../../mcp_tool_schema";
import { type ActionEntry, fixed, fromArgs, plural } from "../action_entry";
import { GMAIL_SERVER_ID } from "../server_ids";

export const gmailActions: Record<string, ActionEntry> = {
  drafts_create: fixed("Gmail: Draft email"),
  threads_modify_labels: fromArgs(mcpToolSchema(GMAIL_SERVER_ID, "threads_modify_labels"), (a) => ({
    text: `Gmail: Relabel ${plural(a.thread_ids.length, "thread")}`,
  })),
  threads_get: fixed("Gmail: Get thread"),
  threads_list: fixed("Gmail: Search threads"),
  messages_get: fixed("Gmail: Get message"),
};
