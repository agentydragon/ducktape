import { callOperatorMcpTool } from "./mcp_client";
import { mcpToolResultSchema, type McpToolResultFor } from "./mcp_tool_result_schema";

const SERVER_ID = "haku_session_sandboxes" as const;
type ActiveSandboxPage = McpToolResultFor<typeof SERVER_ID, "list_active">;
type TerminationResult = McpToolResultFor<typeof SERVER_ID, "terminate">;

const zActiveSandboxPage = mcpToolResultSchema(SERVER_ID, "list_active");
const zTerminationResult = mcpToolResultSchema(SERVER_ID, "terminate");

export type ActiveSandbox = ActiveSandboxPage["items"][number];

export async function listActiveSandboxes(): Promise<ActiveSandbox[]> {
  const page = zActiveSandboxPage.parse(await callOperatorMcpTool(`${SERVER_ID}__list_active`, { limit: 100 }));
  return page.items;
}

export async function terminateSandbox(sessionId: string): Promise<TerminationResult> {
  return zTerminationResult.parse(await callOperatorMcpTool(`${SERVER_ID}__terminate`, { session_id: sessionId }));
}
