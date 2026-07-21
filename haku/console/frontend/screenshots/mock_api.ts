// Installs a canned-response `fetch` for the screenshot harness so data-fetching surfaces
// (the history view) render populated instead of an error. MUST be imported before any
// module that captures `globalThis.fetch` (openapi-fetch does so when client.ts builds its
// client) — harness.tsx imports this first. Paired with a `<base href>` in the harness page
// (render.mjs) so the relative "/api/…" URL parses in the origin-less setContent page.
import {
  SAMPLE_DAEMONS,
  SAMPLE_DEPLOYMENT,
  SAMPLE_MCP_PROBES,
  SAMPLE_MCP_SERVERS,
  SAMPLE_PENDING,
  SAMPLE_TOOL_CALLS,
} from "./sample_data.ts";
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
const scene = (window as unknown as { __SCENE__?: string }).__SCENE__;
const mcpServers =
  scene === "settings-oauth-success"
    ? SAMPLE_MCP_SERVERS.map((server) =>
        server.server_id === "grocy-sf"
          ? {
              ...server,
              connection: {
                server_id: "grocy-sf",
                username: "agentydragon",
                state: {
                  status: "connected" as const,
                  connected_at: "2026-07-20T20:00:00Z",
                  token_expires_at: "2026-08-20T20:00:00Z",
                  scope: "read write",
                },
              },
            }
          : server
      )
    : SAMPLE_MCP_SERVERS;

globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = requestUrl(input);
  if (url.includes("/api/agent-enrollment/agents")) {
    return jsonResponse({
      agents: [
        {
          agent_id: "40000000-0000-4000-8000-000000000004",
          display_name: "Claude Desktop",
          status: "active",
          credential_kind: "oauth",
          credential_status: "active",
          created_at: "2026-07-18T12:00:00Z",
          activated_at: "2026-07-18T12:05:00Z",
          last_seen_at: "2026-07-20T19:30:00Z",
        },
        {
          agent_id: "50000000-0000-4000-8000-000000000005",
          display_name: "Codex",
          status: "active",
          credential_kind: "static",
          credential_status: "active",
          created_at: "2026-07-19T12:00:00Z",
          activated_at: "2026-07-19T12:00:00Z",
          last_seen_at: "2026-07-20T19:34:00Z",
        },
      ],
    });
  }
  if (url.includes("/api/agent-enrollment/")) {
    return jsonResponse({
      operator_display_name: "Rai",
      client_software: "Claude Desktop",
      redirect_host: "localhost:6274",
      requested_scopes: ["openid", "offline_access", "mcp:tools"],
      suggested_agent_name: "Claude Desktop — laptop",
      reconnectable_agents: [{ agent_id: "40000000-0000-4000-8000-000000000004", display_name: "Claude Desktop" }],
      form_token: "form-token-for-screenshot",
    });
  }
  if (url.includes("/api/deployment")) return jsonResponse(SAMPLE_DEPLOYMENT);
  if (url.includes("/api/approvals/pending")) return jsonResponse({ approvals: SAMPLE_PENDING });
  const mcpResponse = await mockOperatorMcpFetch(input, init, url, {
    ...GOOGLE_CALENDAR_MCP_FIXTURES,
    ...GROCY_MCP_FIXTURES,
    list_mcp_servers: () => ({ servers: mcpServers }),
    get_mcp_server_status: (args) => {
      const serverId = String(args.server_id);
      if (scene === "settings-oauth-success" && serverId === "grocy-sf") {
        return {
          connection: mcpServers.find((server) => server.server_id === serverId)!,
          server: { server_id: serverId, title: serverId, state: { status: "alive" as const, tools: [] } },
        };
      }
      return SAMPLE_MCP_PROBES[serverId];
    },
    list_node_daemons: () => ({ daemons: SAMPLE_DAEMONS }),
  });
  if (mcpResponse !== null) return mcpResponse;
  if (url.includes("/api/tool-calls")) return jsonResponse({ tool_calls: SAMPLE_TOOL_CALLS });
  if (realFetch) return realFetch(input, init);
  return jsonResponse({});
}) as typeof fetch;
