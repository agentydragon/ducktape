import { api, errorDetail } from "./client.ts";
import type { components } from "./api/schema";

export type GmailThreadPreview = components["schemas"]["GmailThreadPreview"];

// The gmail write-tool argument types (ModifyGmailThreadLabelsArgs, CreateGmailDraftArgs)
// aren't re-exported here: tool_previews/gmail.tsx gets both the runtime validator and the
// inferred TS type from :schema_zod (api/schema.zod.ts), generated from the same OpenAPI schema
// this file's `components["schemas"]` draws from — see `GmailToolArgumentExamples`
// (haku/console/tools/gmail.py) for why those models reach that schema even though nothing calls
// that endpoint for data.

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
