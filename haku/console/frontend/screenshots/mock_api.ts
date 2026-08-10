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
} from "./sample_data";
import { mockOperatorMcpFetch } from "../tool_rendering/screenshot/mcp_mock";
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

const realFetch = globalThis.fetch;
const scene = (window as unknown as { __SCENE__?: string }).__SCENE__;
const claudeBoundaryMessages = [
  {
    message_id: "61000000-0000-4000-8000-000000000010",
    role: "user",
    status: "complete",
    content: "Try the Haku Console MCP tools.",
    tool_uses: [],
    error: null,
    created_at: "2026-08-01T03:00:10Z",
    updated_at: "2026-08-01T03:00:10Z",
  },
  {
    message_id: "62000000-0000-4000-8000-000000000010",
    role: "assistant",
    status: "complete",
    content: "I'll start with the catalog, then try a read-only query.",
    tool_uses: [],
    error: null,
    created_at: "2026-08-01T03:00:11Z",
    updated_at: "2026-08-01T03:00:11Z",
  },
  {
    message_id: "62000000-0000-4000-8000-000000000011",
    role: "assistant",
    status: "complete",
    content: "",
    tool_uses: [
      {
        tool_use_id: "toolu_01HakuConsoleRead",
        name: "mcp__haku-console__haku-console__list_mcp_servers",
        input: {},
      },
    ],
    error: null,
    created_at: "2026-08-01T03:00:12Z",
    updated_at: "2026-08-01T03:00:12Z",
  },
  {
    message_id: "62000000-0000-4000-8000-000000000012",
    role: "assistant",
    status: "complete",
    content: "The Haku Console catalog is available. Next I'll try the read-only query.",
    tool_uses: [],
    error: null,
    created_at: "2026-08-01T03:00:13Z",
    updated_at: "2026-08-01T03:00:13Z",
  },
] as const;
const standardClaudeMessages = [
  {
    message_id: "61000000-0000-4000-8000-000000000006",
    role: "user",
    status: "complete",
    content: "Create a **short note** in the sandbox and tell me what you wrote.",
    tool_uses: [],
    error: null,
    created_at: "2026-08-01T03:00:10Z",
    updated_at: "2026-08-01T03:00:10Z",
  },
  {
    message_id: "62000000-0000-4000-8000-000000000006",
    role: "assistant",
    status: "complete",
    content:
      "I created `/workspace/note.txt` with:\n\n> Hello from the disposable Haku sandbox.\n\n- Saved locally\n- Ready to inspect",
    tool_uses: [],
    error: null,
    created_at: "2026-08-01T03:00:11Z",
    updated_at: "2026-08-01T03:00:15Z",
  },
] as const;
const overflowingClaudeMessages = Array.from({ length: 8 }, (_, index) => {
  const sequence = String(index + 1).padStart(12, "0");
  return [
    {
      message_id: `61000000-0000-4000-8000-${sequence}`,
      role: "user" as const,
      status: "complete" as const,
      content: `Question **${index + 1}**: inspect the current sandbox state.`,
      tool_uses: [],
      error: null,
      created_at: `2026-08-01T03:00:${String(index * 2).padStart(2, "0")}Z`,
      updated_at: `2026-08-01T03:00:${String(index * 2).padStart(2, "0")}Z`,
    },
    {
      message_id: `62000000-0000-4000-8000-${sequence}`,
      role: "assistant" as const,
      status: "complete" as const,
      content:
        index === 7
          ? "### Latest answer\n\nThe transcript stayed pinned to the newest message."
          : `Answer ${index + 1}: the sandbox is **ready**.`,
      tool_uses: [],
      error: null,
      created_at: `2026-08-01T03:00:${String(index * 2 + 1).padStart(2, "0")}Z`,
      updated_at: `2026-08-01T03:00:${String(index * 2 + 1).padStart(2, "0")}Z`,
    },
  ];
}).flat();
const claudeSession = scene?.startsWith("claude-provisioning")
  ? {
      session_id: "60000000-0000-4000-8000-000000000006",
      status: "provisioning",
      error: null,
      created_at: "2026-08-01T03:00:00Z",
      updated_at: "2026-08-01T03:00:03Z",
      provisioning: {
        step: "waiting_for_pod_ready",
        inspected_at: "2026-08-01T03:00:03Z",
        claim_name: "claude-60000000000040008000000000000006",
        claim_ready: false,
        claim_reason: "PodNotReady",
        claim_message: "Waiting for the sandbox Pod to become ready",
        sandbox_name: "haku-claude-7r9qk",
        sandbox_ready: false,
        pod_name: "haku-claude-7r9qk",
        pod_phase: "Pending",
        pod_ready: false,
        runner_ready: false,
        runner_state: "waiting: ContainerCreating",
        observation_error: null,
      },
      messages: [],
    }
  : ({
      session_id: "60000000-0000-4000-8000-000000000006",
      status: "ready",
      error: null,
      created_at: "2026-08-01T03:00:00Z",
      updated_at: "2026-08-01T03:01:00Z",
      provisioning: null,
      messages:
        scene === "claude-message-boundaries"
          ? claudeBoundaryMessages
          : scene === "claude-chat-overflow"
            ? overflowingClaudeMessages
            : standardClaudeMessages.map((message) =>
                message.role === "assistant" && scene?.startsWith("claude-tool-use")
                  ? {
                      ...message,
                      tool_uses: [
                        {
                          tool_use_id: "toolu_01HakuConsoleRead",
                          name: "mcp__haku-console__haku-console__list_mcp_servers",
                          input: {},
                        },
                        {
                          tool_use_id: "toolu_02WriteNote",
                          name: "Write",
                          input: {
                            file_path: "/workspace/note.txt",
                            content: "Hello from the disposable Haku sandbox.",
                          },
                        },
                      ],
                    }
                  : message
              ),
    } as const);
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
  if (url.includes("/api/agent-enrollment/agents/") && init?.method === "PUT") {
    const body = JSON.parse(String(init.body)) as { auto_approval_policy: string };
    return jsonResponse({
      agent_id: "40000000-0000-4000-8000-000000000004",
      display_name: "Claude Desktop",
      status: "active",
      credential_kind: "oauth",
      credential_status: "active",
      created_at: "2026-07-18T12:00:00Z",
      activated_at: "2026-07-18T12:05:00Z",
      last_seen_at: "2026-07-20T19:30:00Z",
      auto_approval_policy: body.auto_approval_policy,
    });
  }
  if (url.includes("/api/agent-enrollment/agents")) {
    return jsonResponse({
      auto_approval_policies: ["manual_review", "haku_v1"],
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
          auto_approval_policy: "haku_v1",
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
          auto_approval_policy: "manual_review",
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
          auto_approval_policy: "haku_v1",
        },
      ],
      auto_approval_policies: ["manual_review", "haku_v1"],
      default_auto_approval_policy: "manual_review",
      form_token: "form-token-for-screenshot",
    });
  }
  if (url.includes("/api/deployment")) return jsonResponse(SAMPLE_DEPLOYMENT);
  if (url.includes("/api/claude/sessions")) return jsonResponse(claudeSession);
  // Push is configured on this console, and one *other* device is enrolled — the two facts the
  // Notifications section exists to show. The headless browser has no real subscription, so
  // "this browser" renders Off; a second device proves the per-device list renders.
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
    // Mirrors the real GET /api/tool-calls's `auto_approved` server-side filter (mcp_approval.py)
    // so the history screenshot scenes exercise the same request the frontend actually sends.
    const autoApproved = new URLSearchParams(url.split("?")[1] ?? "").get("auto_approved");
    const matching =
      autoApproved === null
        ? SAMPLE_TOOL_CALLS
        : SAMPLE_TOOL_CALLS.filter((call) => (call.approval_policy_id != null) === (autoApproved === "true"));
    // The `history-paged` scene needs a ledger deeper than one page, which the handful of
    // hand-written samples is not: it is what shows the "Load older calls" affordance and the
    // placeholders standing in for the code blocks of rows that are not near the viewport yet.
    // Repeated after filtering, so the page is deep enough under the default `auto_approved=false`
    // the history view actually sends.
    const ledger =
      scene === "history-paged"
        ? Array.from({ length: 26 }, (_unused, index) => ({
            ...matching[index % matching.length],
            tool_call_id: `tc_paged_${index}`,
          }))
        : matching;
    // Mirrors the real endpoint's keyset paging (mcp_approval.py): `cursor` is the opaque position
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
  if (realFetch) return realFetch(input, init);
  return jsonResponse({});
}) as typeof fetch;
