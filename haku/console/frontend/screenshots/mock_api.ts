// Installs a canned-response `fetch` for the screenshot harness so data-fetching surfaces
// (the history view) render populated instead of an error. MUST be imported before any
// module that captures `globalThis.fetch` (openapi-fetch does so when client.ts builds its
// client) — harness.tsx imports this first. Paired with a `<base href>` in the harness page
// (render.mjs) so the relative "/api/…" URL parses in the origin-less setContent page.
import {
  SAMPLE_DAEMONS,
  SAMPLE_DEPLOYMENT,
  SAMPLE_GRANTS,
  SAMPLE_MCP_PROBES,
  SAMPLE_MCP_SERVERS,
  SAMPLE_PENDING,
  SAMPLE_TOOL_CALLS,
  sampleAiquota,
} from "./sample_data";
import { mockOperatorMcpFetch } from "../tool_rendering/screenshot/mcp_mock";
import { ensureLedger, recordViolation, tracked } from "../tool_rendering/screenshot/visual_network_ledger";
import { GOOGLE_CALENDAR_MCP_FIXTURES } from "../tool_rendering/google_calendar/fixtures";
import { GROCY_MCP_FIXTURES } from "../tool_rendering/grocy/fixtures";

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

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

async function respond(input: RequestInfo | URL, init: RequestInit | undefined, url: string): Promise<Response | null> {
  if (url.includes("/api/grants")) return jsonResponse(SAMPLE_GRANTS);
  if (url.includes("/api/agent-enrollment/agents/") && init?.method === "PUT") {
    const body = JSON.parse(String(init.body)) as { access_profile_id: string };
    return jsonResponse({
      agent_id: "40000000-0000-4000-8000-000000000004",
      display_name: "Claude Desktop",
      status: "active",
      credential_kind: "oauth",
      credential_status: "active",
      created_at: "2026-07-18T12:00:00Z",
      activated_at: "2026-07-18T12:05:00Z",
      last_seen_at: "2026-07-20T19:30:00Z",
      access_profile_id: body.access_profile_id,
    });
  }
  if (url.includes("/api/agent-enrollment/agents")) {
    return jsonResponse({
      access_profiles: ["manual_review", "haku_v1"],
      agents: [
        {
          agent_id: "30000000-0000-4000-8000-000000000003",
          display_name: "Public Coder",
          status: "active",
          credential_kind: "static",
          credential_status: "active",
          created_at: "2026-07-18T12:00:00Z",
          activated_at: "2026-07-18T12:00:00Z",
          last_seen_at: "2026-07-20T19:30:00Z",
          access_profile_id: "manual_review",
        },
        {
          agent_id: "40000000-0000-4000-8000-000000000004",
          display_name: "Claude Desktop",
          status: "active",
          credential_kind: "oauth",
          credential_status: "active",
          created_at: "2026-07-18T12:00:00Z",
          activated_at: "2026-07-18T12:05:00Z",
          last_seen_at: "2026-07-20T19:30:00Z",
          access_profile_id: "haku_v1",
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
          access_profile_id: "manual_review",
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
      reconnectable_agents: [
        {
          agent_id: "40000000-0000-4000-8000-000000000004",
          display_name: "Claude Desktop",
          access_profile_id: "haku_v1",
        },
      ],
      access_profiles: ["manual_review", "haku_v1"],
      default_access_profile_id: "manual_review",
      form_token: "form-token-for-screenshot",
    });
  }
  // The framed Haku UI's origin, plus whether the launch-routine capability is configured (kept
  // null since no scene needs it enabled).
  if (url.includes("/api/config")) {
    return jsonResponse({
      launch_routine_url: null,
      haku_ui_url: "https://haku-ui.test",
    });
  }
  if (url.includes("/api/deployment")) return jsonResponse(SAMPLE_DEPLOYMENT);
  if (url.includes("/api/aiquota/quotas")) return jsonResponse(sampleAiquota(Date.now()));
  // Push is configured and one *other* device is enrolled. The headless browser has no real
  // subscription, so "this browser" renders Off while the second device fills the per-device list.
  if (url.includes("/api/push/config")) return jsonResponse({ application_server_key: "BEl62iUYgUivxIkv69yViEuiBIa" });
  if (url.includes("/api/push/subscriptions")) {
    return jsonResponse([
      { endpoint: "https://push.example/phone", user_agent: "Pixel 9 · Chrome", created_at: "2026-07-18T09:00:00Z" },
    ]);
  }
  // Far enough out that the shell's session warning stays hidden; `session-expiring` drives the
  // warned state through ShellChrome props instead, so every other scene renders the calm rail.
  if (url.includes("/auth/me")) {
    return jsonResponse({ username: "agentydragon", expires_at: "2126-07-20T21:00:00Z" });
  }
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
  if (url.includes("/api/tool-calls")) {
    // Mirrors the real GET /api/tool-calls's `auto_approved` server-side filter (mcp/approval.py)
    // so the history screenshot scenes exercise the same request the frontend actually sends.
    const autoApproved = new URLSearchParams(url.split("?")[1] ?? "").get("auto_approved");
    const matching =
      autoApproved === null
        ? SAMPLE_TOOL_CALLS
        : SAMPLE_TOOL_CALLS.filter((call) => (call.approval_policy_id != null) === (autoApproved === "true"));
    // The `history-paged` scene needs a ledger deeper than one page — that is what shows the "Load
    // older calls" affordance and the placeholders standing in for rows not near the viewport yet.
    // Repeated after filtering, so the page is deep enough under the default `auto_approved=false`
    // the history view sends.
    const ledger =
      scene === "history-paged"
        ? Array.from({ length: 26 }, (_unused, index) => ({
            ...matching[index % matching.length],
            tool_call_id: `tc_paged_${index}`,
          }))
        : matching;
    // Mirrors the real endpoint's keyset paging (mcp/approval.py): `cursor` is the opaque position
    // handed out as `next_cursor`, and a full page always offers one.
    const query = new URLSearchParams(url.split("?")[1] ?? "");
    const limit = Number(query.get("limit") ?? 100);
    const cursor = query.get("cursor");
    const start = cursor === null ? 0 : ledger.findIndex((call) => call.tool_call_id === cursor) + 1;
    const toolCalls = ledger.slice(start, start + limit);
    return jsonResponse({
      tool_calls: toolCalls,
      next_cursor: toolCalls.length === limit ? toolCalls[toolCalls.length - 1].tool_call_id : null,
    });
  }
  return null;
}

ensureLedger();
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = requestUrl(input);
  return tracked(url, async () => {
    const response = await respond(input, init, url);
    if (response !== null) return response;
    // A route no mock matches must fail the run, loudly and by name — falling through to the real
    // fetch could only reject in the hermetic sandbox, and answering silently would let a new
    // surface's unmocked request render an empty state nobody chose. The empty answer below keeps
    // the page deterministic for the scene's other assertions while the ledger fails the test.
    recordViolation(`unmatched route: ${url}`);
    return jsonResponse({});
  });
}) as typeof fetch;

// Nothing in this harness serves a live socket, which is what a real browser does here for
// `/api/events/ws` — every socket the shell opens is refused immediately, and the shell renders
// the failure as a sync error. Leaving one hanging instead would hold the shell at "connecting",
// which reads as a spinner that never stops.
class HarnessSocket {
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(_url: string | URL) {
    queueMicrotask(() => {
      this.onerror?.();
      this.onclose?.({ code: 1006, reason: "" });
    });
  }

  close(): void {}
}

globalThis.WebSocket = HarnessSocket as unknown as typeof WebSocket;
