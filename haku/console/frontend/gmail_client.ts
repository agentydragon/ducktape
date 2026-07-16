import { callOperatorMcpTool } from "./mcp_client.ts";
import { mcpToolResultSchema, type McpToolResultFor } from "./mcp_tool_result_schema.ts";

type GmailThreadPreviewsResponse = McpToolResultFor<"gmail", "thread_previews">;
export type GmailThreadPreview = GmailThreadPreviewsResponse["threads"][string];

const zGmailThreadPreviews = mcpToolResultSchema("gmail", "thread_previews");

// The in-process MCP tool batches live subject/snippet/current-label resolution. Operator browser
// calls execute directly through /mcp, so this enrichment creates no approval or audit row.
export async function fetchGmailThreadPreviews(threadIds: string[]): Promise<Record<string, GmailThreadPreview>> {
  if (threadIds.length === 0) return {};
  const payload = await callOperatorMcpTool("gmail_thread_previews", { thread_ids: threadIds });
  return zGmailThreadPreviews.parse(payload).threads;
}
