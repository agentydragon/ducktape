// Card and notification action descriptions for the in-process hostexec server's tools. React-free so `../../sw.ts`
// can bundle them (see ../action_entry.ts).

import { mcpToolSchema } from "../../mcp_tool_schema";
import { type ActionEntry, fromArgs } from "../action_entry";
import { HOSTEXEC_SERVER_ID } from "../server_ids";

export const hostexecActions: Record<string, ActionEntry> = {
  bash: fromArgs(mcpToolSchema(HOSTEXEC_SERVER_ID, "bash"), (a) => ({
    text: `hostexec: Run on ${a.host} as ${a.run_as}`,
  })),
};
