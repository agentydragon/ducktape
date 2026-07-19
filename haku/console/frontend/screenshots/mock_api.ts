// Installs a canned-response `fetch` for the screenshot harness so data-fetching surfaces
// (the history view) render populated instead of an error. MUST be imported before any
// module that captures `globalThis.fetch` (openapi-fetch does so when client.ts builds its
// client) — harness.tsx imports this first. Paired with a `<base href>` in the harness page
// (render.mjs) so the relative "/api/…" URL parses in the origin-less setContent page.
import { SAMPLE_DEPLOYMENT, SAMPLE_MCP, SAMPLE_PROVIDER_CONNECTIONS, SAMPLE_TOOL_CALLS } from "./sample_data.ts";
import { mockOperatorMcpFetch } from "../tool_rendering/screenshot/mcp_mock.ts";
import { GOOGLE_CALENDAR_MCP_FIXTURES } from "../tool_rendering/google_calendar/fixtures.ts";
import { GROCY_MCP_FIXTURES } from "../tool_rendering/grocy/fixtures.ts";

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
  if (url.includes("/api/operator-connections")) return jsonResponse({ connections: SAMPLE_PROVIDER_CONNECTIONS });
  if (url.includes("/api/deployment")) return jsonResponse(SAMPLE_DEPLOYMENT);
  const mcpResponse = await mockOperatorMcpFetch(input, init, url, {
    ...GOOGLE_CALENDAR_MCP_FIXTURES,
    ...GROCY_MCP_FIXTURES,
  });
  if (mcpResponse !== null) return mcpResponse;
  if (url.includes("/api/tool-calls")) return jsonResponse({ tool_calls: SAMPLE_TOOL_CALLS });
  if (realFetch) return realFetch(input, init);
  return jsonResponse({});
}) as typeof fetch;
