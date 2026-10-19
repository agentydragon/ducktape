// Deterministic API responses for shell screenshots. Import before modules that capture fetch.
import { SAMPLE_DEPLOYMENT } from "./sample_data";

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

async function respond(input: RequestInfo | URL, init: RequestInit | undefined, url: string): Promise<Response | null> {
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
  if (url.includes("/api/config")) {
    return jsonResponse({ launch_routine_url: null, haku_ui_url: "https://haku-ui.test" });
  }
  if (url.includes("/api/deployment")) return jsonResponse(SAMPLE_DEPLOYMENT);
  if (url.includes("/auth/me")) {
    return jsonResponse({ username: "agentydragon", expires_at: "2126-07-20T21:00:00Z" });
  }
  return null;
}

globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = requestUrl(input);
  const response = await respond(input, init, url);
  if (response !== null) return response;
  throw new Error(`Unmatched screenshot API request: ${url}`);
}) as typeof fetch;
