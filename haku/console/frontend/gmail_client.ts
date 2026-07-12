import { api, errorDetail } from "./client.ts";
import type { components } from "./api/schema";

export type GmailThreadPreview = components["schemas"]["GmailThreadPreview"];

// Gmail write-tool argument types do not come from this HTTP client's OpenAPI declarations.
// tool_rendering/gmail/requests.tsx gets both its runtime validator and inferred static type from the
// input schemas the FastMCP server itself advertises; see :mcp_tool_schema.

// Live subject/snippet/current-labels lookup for rendering a threads_modify_labels
// approval — the tool call's own arguments only carry thread IDs. Threads the operator's
// account can't resolve (deleted, wrong account, …) are simply absent from the map.
export async function fetchGmailThreadPreviews(threadIds: string[]): Promise<Record<string, GmailThreadPreview>> {
  if (threadIds.length === 0) return {};
  const { data, error } = await api.GET("/api/gmail/thread-previews", {
    params: { query: { thread_id: threadIds } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load Gmail thread previews"));
  return data.threads;
}
