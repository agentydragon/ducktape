// Notification/card action descriptions for the in-process hostexec server's tools — the one-line summary the
// approvals card shows and a push notification is titled with. Beside the widgets they
// describe, and React-free so `../../sw.ts` can bundle them (see ../action_entry.ts).

import { mcpToolSchema } from "../../mcp_tool_schema";
import { type ActionEntry, fromArgs } from "../action_entry";
import { HOSTEXEC_SERVER_ID } from "../server_ids";

export const hostexecActions: Record<string, ActionEntry> = {
  bash: fromArgs(mcpToolSchema(HOSTEXEC_SERVER_ID, "bash"), (a) => ({
    text: `hostexec: Run on ${a.host} as ${a.run_as}`,
  })),
};
