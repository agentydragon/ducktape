import { mcpToolSchema } from "../../mcp_tool_schema";
import { type ActionEntry, fromArgs } from "../action_entry";
import { KUBERNETES_SERVER_ID } from "../server_ids";

const zCreateGrantArgs = mcpToolSchema(KUBERNETES_SERVER_ID, "create_grant");
const zReleaseGrantsArgs = mcpToolSchema(KUBERNETES_SERVER_ID, "release_grants");

function plural(count: number): string {
  return `${count} grant${count === 1 ? "" : "s"}`;
}

export const kubernetesActions: Record<string, ActionEntry> = {
  create_grant: fromArgs(zCreateGrantArgs, (args) => ({
    text: `Kubernetes: Create ${plural(args.grants.length)}`,
  })),
  release_grants: fromArgs(zReleaseGrantsArgs, (args) => ({
    text: `Kubernetes: Release ${plural(args.grant_ids.length)}`,
    destructive: true,
  })),
};
