import { mcpToolSchema } from "../../mcp_tool_schema";
import { type ActionEntry, fromArgs } from "../action_entry";
import { GRANTS_SERVER_ID } from "../server_ids";

const zCreateGrantArgs = mcpToolSchema(GRANTS_SERVER_ID, "create_grant");
const zReleaseGrantsArgs = mcpToolSchema(GRANTS_SERVER_ID, "release_grants");

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
  release_grants: fromArgs(zReleaseGrantsArgs, (args) => ({
    text: `Grants: Release ${domainLabel(args.domain)} ${plural(args.grant_ids.length)}`,
    destructive: true,
  })),
};
