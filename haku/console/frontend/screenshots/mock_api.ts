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
type MockItem = {
  item_id: string;
  item_type: "prompt" | "message" | "reasoning" | "tool_call";
  status: "open" | "complete" | "failed";
  text: string;
  call_id: string | null;
  tool_name: string | null;
  arguments: Record<string, unknown> | null;
  outcome: "succeeded" | "failed" | "unknown" | null;
  structured: unknown;
  disclosure: "summary" | "withheld" | null;
  created_at: string;
  updated_at: string;
};

function spoke(
  item_id: string,
  item_type: "prompt" | "message",
  text: string,
  at: string,
  until: string = at
): MockItem {
  return {
    item_id,
    item_type,
    status: "complete",
    text,
    call_id: null,
    tool_name: null,
    arguments: null,
    outcome: null,
    structured: null,
    disclosure: null,
    created_at: at,
    updated_at: until,
  };
}

/** A tool call as one item: its ask and its answer on the same row, still open while it runs. */
function called(
  item_id: string,
  call_id: string,
  tool_name: string,
  args: Record<string, unknown>,
  at: string,
  answer?: { text: string; outcome: "succeeded" | "failed" }
): MockItem {
  return {
    item_id,
    item_type: "tool_call",
    status: answer ? "complete" : "open",
    text: answer?.text ?? "",
    call_id,
    tool_name,
    arguments: args,
    outcome: answer?.outcome ?? null,
    structured: null,
    disclosure: null,
    created_at: at,
    updated_at: at,
  };
}

const EDIT_ARGUMENTS = {
  file_path: "/workspace/src/renderer.ts",
  old_string: "const transcript = items.map(renderItem);\n".repeat(16),
  new_string: "const transcript = items.map(renderConversationItem);\n".repeat(16),
};
const DIFF_CHECK_OUTPUT = Array.from(
  { length: 14 },
  (_unused, line) => `checked generated file ${line + 1}: no whitespace errors`
).join("\n");

const boundaryItems: readonly MockItem[] = [
  spoke("61000000-0000-4000-8000-000000000010", "prompt", "Try the Haku Console MCP tools.", "2026-08-01T03:00:10Z"),
  spoke(
    "62000000-0000-4000-8000-000000000010",
    "message",
    "I'll start with the catalog, then try a read-only query.",
    "2026-08-01T03:00:11Z"
  ),
  called(
    "63000000-0000-4000-8000-000000000010",
    "toolu_01HakuConsoleRead",
    "mcp__haku-console__haku-console__list_mcp_servers",
    {},
    "2026-08-01T03:00:12Z",
    {
      text: '{"servers": [{"server_id": "gmail", "status": "alive"}, {"server_id": "tana", "status": "degraded"}]}',
      outcome: "succeeded",
    }
  ),
  called(
    "63000000-0000-4000-8000-000000000011",
    "toolu_01EditTranscript",
    "Edit",
    EDIT_ARGUMENTS,
    "2026-08-01T03:00:12Z",
    {
      text: "Updated /workspace/src/renderer.ts",
      outcome: "succeeded",
    }
  ),
  called(
    "63000000-0000-4000-8000-000000000012",
    "toolu_02BashTranscript",
    "Bash",
    { command: "git diff --check" },
    "2026-08-01T03:00:12Z",
    { text: DIFF_CHECK_OUTPUT, outcome: "succeeded" }
  ),
  spoke(
    "62000000-0000-4000-8000-000000000012",
    "message",
    "The Haku Console catalog is available. Next I'll try the read-only query.",
    "2026-08-01T03:00:13Z"
  ),
];
const standardItems: readonly MockItem[] = [
  spoke(
    "61000000-0000-4000-8000-000000000006",
    "prompt",
    "Create a **short note** in the sandbox and tell me what you wrote.",
    "2026-08-01T03:00:10Z"
  ),
  spoke(
    "62000000-0000-4000-8000-000000000006",
    "message",
    "I created `/workspace/note.txt` with:\n\n> Hello from the disposable Haku sandbox.\n\n- Saved locally\n- Ready to inspect",
    "2026-08-01T03:00:11Z",
    "2026-08-01T03:00:15Z"
  ),
];
const overflowingItems: readonly MockItem[] = Array.from({ length: 8 }, (_unused, index) => {
  const sequence = String(index + 1).padStart(12, "0");
  const asked = `2026-08-01T03:00:${String(index * 2).padStart(2, "0")}Z`;
  const answered = `2026-08-01T03:00:${String(index * 2 + 1).padStart(2, "0")}Z`;
  return [
    spoke(
      `61000000-0000-4000-8000-${sequence}`,
      "prompt",
      `Question **${index + 1}**: inspect the current sandbox state.`,
      asked
    ),
    spoke(
      `62000000-0000-4000-8000-${sequence}`,
      "message",
      index === 7
        ? "### Latest answer\n\nThe transcript stayed pinned to the newest item."
        : `Answer ${index + 1}: the sandbox is **ready**.`,
      answered
    ),
  ];
}).flat();
// The same transcript with every state a tool call can be in: answered, failed, still running, and
// one long enough to need its own scroll.
const toolUsingItems: readonly MockItem[] = [
  ...standardItems,
  called(
    "63000000-0000-4000-8000-000000000006",
    "toolu_01HakuConsoleRead",
    "mcp__haku-console__haku-console__list_mcp_servers",
    {},
    "2026-08-01T03:00:12Z",
    {
      text: '{"servers": [{"server_id": "gmail", "status": "alive"}, {"server_id": "tana", "status": "degraded"}]}',
      outcome: "succeeded",
    }
  ),
  called(
    "63000000-0000-4000-8000-000000000007",
    "toolu_02WriteNote",
    "Write",
    { file_path: "/workspace/note.txt", content: "Hello from the disposable Haku sandbox." },
    "2026-08-01T03:00:12Z",
    { text: "EACCES: permission denied, open '/workspace/note.txt'", outcome: "failed" }
  ),
  called(
    "63000000-0000-4000-8000-000000000008",
    "toolu_03StillRunning",
    "Bash",
    { command: "rg --files | wc -l" },
    "2026-08-01T03:00:13Z"
  ),
  called("63000000-0000-4000-8000-000000000009", "toolu_04Edit", "Edit", EDIT_ARGUMENTS, "2026-08-01T03:00:13Z", {
    text: "Updated /workspace/src/renderer.ts",
    outcome: "succeeded",
  }),
  called(
    "63000000-0000-4000-8000-00000000000a",
    "toolu_05BashOutput",
    "Bash",
    { command: "git diff --check" },
    "2026-08-01T03:00:14Z",
    { text: DIFF_CHECK_OUTPUT, outcome: "succeeded" }
  ),
];
const conversationId = "70000000-0000-4000-8000-000000000001";
const conversationSessionId = "70000000-0000-4000-8000-000000000011";
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
// One conversation per row, with the channels holding it rather than one surface: the first is a
// room with a live session, the second a room between runners, the third a browser thread nothing
// is attached to.
const conversationPage = {
  conversations: [
    {
      conversation_id: conversationId,
      created_at: "2026-08-01T03:00:00Z",
      last_activity_at: "2026-08-01T03:01:00Z",
      attachments: [{ surface: "matrix", address: "!ops:example.org", attached_at: "2026-08-01T03:00:00Z" }],
      live_session: { session_id: conversationSessionId, status: "ready" },
      item_count: 6,
    },
    {
      conversation_id: "70000000-0000-4000-8000-0000000000a2",
      created_at: "2026-07-31T18:20:00Z",
      last_activity_at: "2026-07-31T18:42:00Z",
      attachments: [{ surface: "matrix", address: "!archive:example.org", attached_at: "2026-07-31T18:20:00Z" }],
      live_session: null,
      item_count: 8,
    },
    {
      conversation_id: "70000000-0000-4000-8000-0000000000a3",
      created_at: "2026-07-30T09:10:00Z",
      last_activity_at: "2026-07-30T09:12:00Z",
      attachments: [],
      live_session: { session_id: "70000000-0000-4000-8000-000000000003", status: "failed" },
      item_count: 2,
    },
  ],
  // Not the last page, so the keyset's "Load older conversations" control renders.
  next_cursor: { last_activity_at: "2026-07-29T22:05:00Z", conversation_id: "70000000-0000-4000-8000-0000000000a4" },
} as const;
// Two exchanges, so the detail scene shows a turn boundary landing between them rather than a
// single marker that could sit anywhere and still look right.
const conversationItems: readonly MockItem[] = [
  ...boundaryItems,
  spoke(
    "61000000-0000-4000-8000-000000000011",
    "prompt",
    "Now check whether the degraded server recovered.",
    "2026-08-01T03:00:20Z"
  ),
  spoke(
    "62000000-0000-4000-8000-000000000013",
    "message",
    "The reflection call timed out before I could answer.",
    "2026-08-01T03:00:24Z"
  ),
];
const conversationSession = {
  session_id: conversationSessionId,
  status: "ready",
  error: null,
  created_at: "2026-08-01T03:00:00Z",
  updated_at: "2026-08-01T03:01:00Z",
  provisioning: null,
  narration: setupNarration,
  items: conversationItems,
  // Newest first, as the endpoint returns them — the transcript numbers them the other way.
  turns: [
    {
      turn_id: "71000000-0000-4000-8000-000000000002",
      started_at: "2026-08-01T03:00:20.4Z",
      ended_at: "2026-08-01T03:00:24Z",
      outcome: "failed",
    },
    {
      turn_id: "71000000-0000-4000-8000-000000000001",
      started_at: "2026-08-01T03:00:10.4Z",
      ended_at: "2026-08-01T03:00:13Z",
      outcome: "answered",
    },
  ],
} as const;
const conversationDetail = {
  conversation_id: conversationId,
  created_at: "2026-08-01T03:00:00Z",
  attachments: [{ surface: "matrix", address: "!ops:example.org", attached_at: "2026-08-01T03:00:00Z" }],
  session: conversationSession,
  // The thread ran one session before this one: what a sandbox dying looks like from the
  // conversation's side, and the only place its frame log stays reachable from.
  earlier_sessions: [
    { session_id: "70000000-0000-4000-8000-000000000010", status: "failed", created_at: "2026-07-31T22:14:00Z" },
  ],
} as const;
// The same session a few seconds earlier: still provisioning, mid-clone, with nothing but the
// narration to show.
const conversationBootstrap = {
  ...conversationDetail,
  session: {
    ...conversationSession,
    status: "provisioning",
    updated_at: "2026-08-01T02:59:51Z",
    narration: setupNarration.slice(0, 4),
    items: [],
    turns: [],
  },
} as const;
// A sandbox still being handed out, where the live Kubernetes read is the whole account for a
// session that never comes up.
const conversationProvisioning = {
  ...conversationDetail,
  session: {
    ...conversationSession,
    status: "provisioning",
    updated_at: "2026-08-01T03:00:03Z",
    provisioning: {
      step: "waiting_for_pod_ready",
      inspected_at: "2026-08-01T03:00:03Z",
      claim_name: "claude-70000000000040008000000000000011",
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
    narration: [],
    items: [],
    turns: [],
  },
} as const;
// A finished session short enough that the collapsed panel stays on screen: the detail scene's
// transcript opens scrolled to its newest message, which puts the collapsed panel above the fold.
const conversationNarrationCollapsed = {
  ...conversationDetail,
  session: { ...conversationSession, items: standardItems, turns: [] },
} as const;
// A transcript long enough to overflow its viewport, so the scroll stays pinned to the newest
// message rather than opening at the top.
const conversationOverflow = {
  ...conversationDetail,
  session: { ...conversationSession, narration: [], items: overflowingItems, turns: [] },
} as const;
// Tool calls with their results, which is what the transcript's card rendering exists for.
const conversationToolUse = {
  ...conversationDetail,
  session: { ...conversationSession, narration: [], items: toolUsingItems, turns: [] },
} as const;
const conversationDetailForScene = scene?.startsWith("conversation-bootstrap")
  ? conversationBootstrap
  : scene?.startsWith("conversation-provisioning")
    ? conversationProvisioning
    : scene?.startsWith("conversation-narration")
      ? conversationNarrationCollapsed
      : scene?.startsWith("conversation-overflow")
        ? conversationOverflow
        : scene?.startsWith("conversation-tool-use")
          ? conversationToolUse
          : conversationDetail;
// The rollout behind that conversation, as the frame inspector reads it: one exchange in wire
// order, with a tool call and the result it got, as the frames themselves carried them.
const conversationFrames = {
  conversation_id: conversationId,
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
  if (url.includes("/api/conversations")) return jsonResponse(conversationPage);
  // The refusal the composer has to render: `enqueue_prompt` answers 409 and records nothing, so
  // the operator's text has to survive it. Before the session read below, whose path this extends.
  if (scene === "conversation-prompt-refused" && url.includes("/messages")) {
    return new Response(JSON.stringify({ detail: "a prompt is already queued" }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    });
  }
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
    // Mirrors the real GET /api/tool-calls's `auto_approved` server-side filter (mcp_approval.py)
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

// The conversation detail page follows a socket rather than fetching, so the canned answer above
// reaches it through one: a single snapshot carrying the same fixture, then silence.
//
// Every other socket is **refused**, which is what a real browser does here — nothing in this
// harness serves `/api/events/ws`, and the shell renders the failure as a sync error. Leaving one
// hanging instead would hold the shell at "connecting", which reads as a spinner that never stops.
class HarnessSocket {
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string | URL) {
    const follows = String(url).includes("/follow");
    queueMicrotask(() => {
      if (!follows) {
        this.onerror?.();
        this.onclose?.({ code: 1006, reason: "" });
        return;
      }
      this.onopen?.();
      this.onmessage?.({
        data: JSON.stringify({ message_type: "snapshot", position: 1, conversation: conversationDetailForScene }),
      });
    });
  }

  close(): void {}
}

globalThis.WebSocket = HarnessSocket as unknown as typeof WebSocket;
