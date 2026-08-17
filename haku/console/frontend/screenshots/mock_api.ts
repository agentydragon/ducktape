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
    tool_calls: [],
    error: null,
    created_at: "2026-08-01T03:00:10Z",
    updated_at: "2026-08-01T03:00:10Z",
  },
  {
    message_id: "62000000-0000-4000-8000-000000000010",
    role: "assistant",
    status: "complete",
    content: "I'll start with the catalog, then try a read-only query.",
    tool_calls: [],
    error: null,
    created_at: "2026-08-01T03:00:11Z",
    updated_at: "2026-08-01T03:00:11Z",
  },
  {
    message_id: "62000000-0000-4000-8000-000000000011",
    role: "assistant",
    status: "complete",
    content: "",
    tool_calls: [
      {
        call_id: "toolu_01HakuConsoleRead",
        tool_name: "mcp__haku-console__haku-console__list_mcp_servers",
        arguments: {},
        result: {
          content:
            '{"servers": [{"server_id": "gmail", "status": "alive"}, {"server_id": "tana", "status": "degraded"}]}',
          is_error: false,
        },
      },
      {
        call_id: "toolu_01EditTranscript",
        tool_name: "Edit",
        arguments: {
          file_path: "/workspace/src/renderer.ts",
          old_string: "const transcript = messages.map(renderMessage);\n".repeat(16),
          new_string: "const transcript = messages.map(renderClaudeMessage);\n".repeat(16),
        },
        result: { content: "Updated /workspace/src/renderer.ts", is_error: false },
      },
      {
        call_id: "toolu_02BashTranscript",
        tool_name: "Bash",
        arguments: { command: "git diff --check" },
        result: {
          content: Array.from(
            { length: 14 },
            (_unused, line) => `checked generated file ${line + 1}: no whitespace errors`
          ).join("\n"),
          is_error: false,
        },
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
    tool_calls: [],
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
    tool_calls: [],
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
    tool_calls: [],
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
      tool_calls: [],
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
      tool_calls: [],
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
                      tool_calls: [
                        {
                          call_id: "toolu_01HakuConsoleRead",
                          tool_name: "mcp__haku-console__haku-console__list_mcp_servers",
                          arguments: {},
                          result: {
                            content:
                              '{"servers": [{"server_id": "gmail", "status": "alive"}, {"server_id": "tana", "status": "degraded"}]}',
                            is_error: false,
                          },
                        },
                        {
                          call_id: "toolu_02WriteNote",
                          tool_name: "Write",
                          arguments: {
                            file_path: "/workspace/note.txt",
                            content: "Hello from the disposable Haku sandbox.",
                          },
                          // A failed call and a still-running one, so the scene shows all three
                          // states a result can be in.
                          result: {
                            content: "EACCES: permission denied, open '/workspace/note.txt'",
                            is_error: true,
                          },
                        },
                        {
                          call_id: "toolu_03StillRunning",
                          tool_name: "Bash",
                          arguments: { command: "rg --files | wc -l" },
                        },
                        {
                          call_id: "toolu_04Edit",
                          tool_name: "Edit",
                          arguments: {
                            file_path: "/workspace/src/renderer.ts",
                            old_string: "const transcript = messages.map(renderMessage);\n".repeat(16),
                            new_string: "const transcript = messages.map(renderClaudeMessage);\n".repeat(16),
                          },
                          result: { content: "Updated /workspace/src/renderer.ts", is_error: false },
                        },
                        {
                          call_id: "toolu_05BashOutput",
                          tool_name: "Bash",
                          arguments: { command: "git diff --check" },
                          result: {
                            content: Array.from(
                              { length: 14 },
                              (_unused, line) => `checked generated file ${line + 1}: no whitespace errors`
                            ).join("\n"),
                            is_error: false,
                          },
                        },
                      ],
                    }
                  : message
              ),
    } as const);
const conversationSessionId = "70000000-0000-4000-8000-000000000001";
// What the shared sandbox bootstrap script writes, forwarded verbatim by the runner — long,
// unbroken paths included, since those are what a narrow viewport has to wrap rather than
// scroll sideways.
const setupNarration = [
  {
    frame_seq: 1,
    text: "+ install -m 600 /var/run/secrets/haku/netrc /root/.netrc",
    created_at: "2026-08-01T02:59:41Z",
  },
  { frame_seq: 2, text: "Cloning into '/workspace/haku-state'...", created_at: "2026-08-01T02:59:42Z" },
  { frame_seq: 3, text: "remote: Enumerating objects: 4821, done.", created_at: "2026-08-01T02:59:44Z" },
  {
    frame_seq: 4,
    text: "Receiving objects: 100% (4821/4821), 12.44 MiB | 8.30 MiB/s, done.",
    created_at: "2026-08-01T02:59:51Z",
  },
  { frame_seq: 5, text: "Resolving deltas: 100% (2610/2610), done.", created_at: "2026-08-01T02:59:52Z" },
  {
    frame_seq: 6,
    text: "Workspace ready at /workspace/haku-state (tip 9f2c1ab8d4e05137c2a9b6f1e83d47a0c5b29e6f).",
    created_at: "2026-08-01T02:59:53Z",
  },
] as const;
const conversationSummaries = [
  {
    session_id: conversationSessionId,
    surface: "matrix",
    room_id: "!ops:example.org",
    status: "ready",
    error: null,
    created_at: "2026-08-01T03:00:00Z",
    updated_at: "2026-08-01T03:01:00Z",
    message_count: 6,
    last_message_at: "2026-08-01T03:00:24Z",
  },
  {
    session_id: "70000000-0000-4000-8000-000000000002",
    surface: "matrix",
    room_id: "!archive:example.org",
    status: "closed",
    error: null,
    created_at: "2026-07-31T18:20:00Z",
    updated_at: "2026-07-31T18:42:00Z",
    message_count: 8,
    last_message_at: "2026-07-31T18:41:00Z",
  },
  {
    session_id: "70000000-0000-4000-8000-000000000003",
    surface: "spa",
    room_id: null,
    status: "failed",
    error: "Sandbox runner stopped unexpectedly",
    created_at: "2026-07-30T09:10:00Z",
    updated_at: "2026-07-30T09:12:00Z",
    message_count: 2,
    last_message_at: "2026-07-30T09:11:00Z",
  },
] as const;
// Two exchanges, so the detail scene shows a turn boundary landing between them rather than a
// single marker that could sit anywhere and still look right.
const conversationMessages = [
  ...claudeBoundaryMessages,
  {
    message_id: "61000000-0000-4000-8000-000000000011",
    role: "user",
    status: "complete",
    content: "Now check whether the degraded server recovered.",
    tool_calls: [],
    error: null,
    created_at: "2026-08-01T03:00:20Z",
    updated_at: "2026-08-01T03:00:20Z",
  },
  {
    message_id: "62000000-0000-4000-8000-000000000013",
    role: "assistant",
    status: "complete",
    content: "The reflection call timed out before I could answer.",
    tool_calls: [],
    error: null,
    created_at: "2026-08-01T03:00:24Z",
    updated_at: "2026-08-01T03:00:24Z",
  },
] as const;
const conversationDetail = {
  session_id: conversationSessionId,
  surface: "matrix",
  room_id: "!ops:example.org",
  status: "ready",
  error: null,
  created_at: "2026-08-01T03:00:00Z",
  updated_at: "2026-08-01T03:01:00Z",
  narration: setupNarration,
  messages: conversationMessages,
  // Newest first, as the endpoint returns them — the transcript numbers them the other way.
  turns: [
    {
      turn_id: "71000000-0000-4000-8000-000000000002",
      started_at: "2026-08-01T03:00:20.4Z",
      ended_at: "2026-08-01T03:00:24Z",
      outcome: "failed",
      usage: { input_tokens: 1900, output_tokens: 60, cached_input_tokens: 0, cost_usd: 0.0041, duration_ms: 3600 },
    },
    {
      turn_id: "71000000-0000-4000-8000-000000000001",
      started_at: "2026-08-01T03:00:10.4Z",
      ended_at: "2026-08-01T03:00:13Z",
      outcome: "answered",
      usage: { input_tokens: 1200, output_tokens: 240, cached_input_tokens: 0, cost_usd: 0.0123, duration_ms: 3200 },
    },
  ],
} as const;
// The same session a few seconds earlier: still provisioning, mid-clone, with nothing but the
// narration to show. This is what the panel exists for, so it gets its own scene.
const conversationBootstrap = {
  ...conversationDetail,
  status: "provisioning",
  updated_at: "2026-08-01T02:59:51Z",
  narration: setupNarration.slice(0, 4),
  messages: [],
  turns: [],
} as const;
// A finished session short enough that the collapsed panel stays on screen: the detail scene's
// transcript opens scrolled to its newest message, which puts the collapsed panel above the fold.
const conversationNarrationCollapsed = {
  ...conversationDetail,
  messages: standardClaudeMessages,
  turns: [],
} as const;
const conversationDetailForScene = scene?.startsWith("conversation-bootstrap")
  ? conversationBootstrap
  : scene?.startsWith("conversation-narration")
    ? conversationNarrationCollapsed
    : conversationDetail;
// The rollout behind that conversation, as the frame inspector reads it: one exchange in wire
// order, with a tool call and the result it got — the pair `session_messages` cannot show.
const conversationFrames = {
  frames: [
    {
      frame_seq: 412,
      direction: "to_agent",
      kind: "user",
      created_at: "2026-08-01T03:00:20Z",
      payload: { type: "user", message: { role: "user", content: "Now check whether the degraded server recovered." } },
    },
    {
      frame_seq: 413,
      direction: "from_agent",
      kind: "assistant",
      created_at: "2026-08-01T03:00:21Z",
      payload: {
        type: "assistant",
        message: {
          id: "msg_01HZ4kQ",
          role: "assistant",
          model: "claude-opus-5",
          content: [
            { type: "text", text: "Reflecting the server now." },
            {
              type: "tool_use",
              id: "toolu_01Rk",
              name: "mcp__haku-console__get_mcp_server_status",
              input: { server_id: "grocy-sf", include_tool_schemas: false },
            },
          ],
        },
      },
    },
    {
      frame_seq: 414,
      direction: "from_agent",
      kind: "user",
      created_at: "2026-08-01T03:00:23Z",
      payload: {
        type: "user",
        message: {
          role: "user",
          content: [
            {
              type: "tool_result",
              tool_use_id: "toolu_01Rk",
              is_error: true,
              content: "McpError: reflection timed out after 30s contacting https://grocy-sf.allegedly.works/mcp",
            },
          ],
        },
      },
    },
    {
      frame_seq: 416,
      direction: "from_agent",
      kind: "result",
      created_at: "2026-08-01T03:00:24Z",
      payload: {
        type: "result",
        subtype: "error_during_execution",
        is_error: true,
        duration_ms: 3600,
        total_cost_usd: 0.0041,
        usage: { input_tokens: 1900, output_tokens: 60 },
      },
    },
  ],
  next_before_seq: 412,
} as const;
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
  // Before the conversation detail below, which its path is a prefix of.
  if (url.includes("/frames")) return jsonResponse(conversationFrames);
  if (url.includes("/api/conversations/")) return jsonResponse(conversationDetailForScene);
  if (url.includes("/api/conversations")) return jsonResponse(conversationSummaries);
  if (url.includes("/api/sessions")) return jsonResponse(claudeSession);
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
