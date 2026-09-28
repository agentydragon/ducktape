import { mcpToolSchema } from "../../mcp_tool_schema";
import { type ActionEntry, fromArgs } from "../action_entry";
import { GRANTS_SERVER_ID } from "../server_ids";

const zCreateGrantArgs = mcpToolSchema(GRANTS_SERVER_ID, "create_grant");
const zRevokeGrantsArgs = mcpToolSchema(GRANTS_SERVER_ID, "revoke_grants");

function plural(count: number): string {
  return `${count} grant${count === 1 ? "" : "s"}`;
}

export const grantsActions: Record<string, ActionEntry> = {
  create_grant: fromArgs(zCreateGrantArgs, (args) => ({
    text: `Grants: Create Kubernetes ${plural(args.grants.length)}`,
  })),
  revoke_grants: fromArgs(zRevokeGrantsArgs, (args) => ({
    text: `Grants: End ${plural(args.grant_ids.length)}`,
    destructive: true,
  })),
};
