import { api, errorDetail } from "./client.ts";
import type { components } from "./api/schema";

export type GmailThreadPreview = components["schemas"]["GmailThreadPreview"];

// The google tool argument types (EventDateTime, CreateCalendarEventArgs, ...) aren't
// re-exported here: google_tool_previews.tsx gets both the runtime validator and the
// inferred TS type from :schema_zod (api/schema.zod.ts), generated from the same
// OpenAPI schema this file's `components["schemas"]` draws from — see
// `GoogleToolArgumentExamples` in haku/console/tools/google.py for why these models
// reach that schema even though nothing calls that endpoint for data.

// Live subject/snippet/current-labels lookup for rendering a batch_modify_gmail_thread_labels
// approval — the tool call's own arguments only carry thread IDs. Threads the operator's
// account can't resolve (deleted, wrong account, …) are simply absent from the map.
export async function fetchGmailThreadPreviews(threadIds: string[]): Promise<Record<string, GmailThreadPreview>> {
  if (threadIds.length === 0) return {};
  const { data, error } = await api.GET("/api/google/gmail/thread-previews", {
    params: { query: { thread_id: threadIds } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load Gmail thread previews"));
  return data.threads;
}
