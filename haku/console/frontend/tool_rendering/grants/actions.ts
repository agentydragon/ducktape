import { mcpToolSchema } from "../../mcp_tool_schema";
import { type ActionEntry, fromArgs } from "../action_entry";
import { GRANTS_SERVER_ID } from "../server_ids";

const zCreateGrantArgs = mcpToolSchema(GRANTS_SERVER_ID, "create_grant");
const zRevokeGrantsArgs = mcpToolSchema(GRANTS_SERVER_ID, "revoke_grants");

function plural(count: number): string {
  return `${count} grant${count === 1 ? "" : "s"}`;
}

function domainLabel(domain: "kubernetes" | "http"): string {
  return domain === "kubernetes" ? "Kubernetes" : "HTTP";
}

export const grantsActions: Record<string, ActionEntry> = {
  create_grant: fromArgs(zCreateGrantArgs, (args) => {
    const domain = args.grants.length > 0 ? `${domainLabel(args.grants[0].domain)} ` : "";
    return { text: `Grants: Create ${domain}${plural(args.grants.length)}` };
  }),
  // One end-grants tool: an Operator naming owner_agent_id revokes, an Agent omitting it releases.
  revoke_grants: fromArgs(zRevokeGrantsArgs, (args) => ({
    text: `Grants: ${args.owner_agent_id ? "Revoke" : "Release"} ${domainLabel(args.domain)} ${plural(args.grant_ids.length)}`,
    destructive: true,
  })),
};
