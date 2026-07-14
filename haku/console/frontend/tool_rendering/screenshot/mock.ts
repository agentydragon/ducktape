// Installs a canned-response `fetch` for the per-server preview screenshot harnesses so the
// data-fetching preview widgets (gmail subjects, grocy reference, calendar name, tana nodes)
// render resolved instead of erroring. MUST be imported before any module that captures
// `globalThis.fetch` — mount.tsx imports this first, before card.tsx (whose widget graph reaches
// client.ts's openapi-fetch). Paired with the `<base href>` render.mjs injects so the relative
// "/api/…" URL parses in the origin-less setContent page.
import {
  SAMPLE_CALENDAR_SUMMARY,
  SAMPLE_GMAIL_THREADS,
  SAMPLE_GROCY_REFERENCE,
  SAMPLE_TANA_NODES,
} from "./mock_data.ts";

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
  // The grocy widgets resolve id→name references and read products' current field values for
  // the products_edit old→new diff — the sample reference carries both.
  if (url.includes("/api/grocy-sf/reference")) return jsonResponse(SAMPLE_GROCY_REFERENCE);
  if (url.includes("/api/tana-rw/node-previews")) return jsonResponse(SAMPLE_TANA_NODES);
  // The Gmail thread-labels widget looks up subjects/labels for its thread ids.
  if (url.includes("/api/gmail/thread-previews")) return jsonResponse({ threads: SAMPLE_GMAIL_THREADS });
  // The create-event widget resolves a non-primary calendar_id to its display name + link.
  if (url.includes("/api/google-calendar/calendar-summary")) return jsonResponse(SAMPLE_CALENDAR_SUMMARY);
  if (realFetch) return realFetch(input, init);
  return jsonResponse({});
}) as typeof fetch;
