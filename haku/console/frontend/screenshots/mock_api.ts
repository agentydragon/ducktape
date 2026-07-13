// Installs a canned-response `fetch` for the screenshot harness so data-fetching surfaces
// (the history view) render populated instead of an error. MUST be imported before any
// module that captures `globalThis.fetch` (openapi-fetch does so when client.ts builds its
// client) — harness.tsx imports this first. Paired with a `<base href>` in the harness page
// (render.mjs) so the relative "/api/…" URL parses in the origin-less setContent page.
import {
  SAMPLE_CALENDAR_SUMMARY,
  SAMPLE_DEPLOYMENT,
  SAMPLE_GMAIL_THREADS,
  SAMPLE_GROCY_REFERENCE,
  SAMPLE_MCP,
  SAMPLE_TOOL_CALLS,
} from "./sample_data.ts";

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

const realFetch = globalThis.fetch;

globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = requestUrl(input);
  // Order matters: match the more specific /api/mcp/... path before /api/tool-calls.
  if (url.includes("/api/mcp/operator-auth")) return jsonResponse({ associations: SAMPLE_MCP });
  if (url.includes("/api/deployment")) return jsonResponse(SAMPLE_DEPLOYMENT);
  // The grocy widgets resolve id→name references and read products' current field values for
  // the products_edit old→new diff — the sample reference carries both.
  if (url.includes("/api/grocy-sf/reference")) return jsonResponse(SAMPLE_GROCY_REFERENCE);
  if (url.includes("/api/tana-rw/node-previews"))
    return jsonResponse({
      nodes: [
        { id: "inbox", name: "Inbox" },
        { id: "task", name: "Quarterly planning" },
        { id: "project", name: "Console project" },
        { id: "old-parent", name: "Backlog" },
      ],
    });
  // The Gmail thread-labels widget looks up subjects/labels for its thread ids.
  if (url.includes("/api/gmail/thread-previews")) return jsonResponse({ threads: SAMPLE_GMAIL_THREADS });
  // The create-event widget resolves a non-primary calendar_id to its display name + link.
  if (url.includes("/api/google-calendar/calendar-summary")) return jsonResponse(SAMPLE_CALENDAR_SUMMARY);
  if (url.includes("/api/tool-calls")) return jsonResponse({ tool_calls: SAMPLE_TOOL_CALLS });
  if (realFetch) return realFetch(input, init);
  return jsonResponse({});
}) as typeof fetch;
