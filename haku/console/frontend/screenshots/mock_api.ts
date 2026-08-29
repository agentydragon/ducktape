// Installs a canned-response `fetch` for the screenshot harness so data-fetching surfaces
// (the history view) render populated instead of an error. MUST be imported before any
// module that captures `globalThis.fetch` (openapi-fetch does so when client.ts builds its
// client) — harness.tsx imports this first. Paired with a `<base href>` in the harness page
// (render.mjs) so the relative "/api/…" URL parses in the origin-less setContent page.
import {
  SAMPLE_DAEMONS,
  SAMPLE_DEPLOYMENT,
  SAMPLE_ACTIVE_SANDBOXES,
  SAMPLE_INDEX_STATUS,
  SAMPLE_GRANTS,
  SAMPLE_MCP_PROBES,
  SAMPLE_MCP_SERVERS,
  SAMPLE_PENDING,
  SAMPLE_TOOL_CALLS,
} from "./sample_data";
import type { Conversation, Item, ConversationPage, SessionFramePage } from "../client";
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

/** Item builders over a running stream position, so a fixture reads in opening order. */
let seq = 0;
function next(): number {
  seq += 10;
  return seq;
}

const AUTHORED = { kind: "authored" } as const;

type Lifecycle = Item["status"];

/** The session waking itself — a prompt nobody typed, whose text says what woke it. */
function woke(text: string): Item {
  const opened = next();
  return {
    kind: "prompt",
    opened_seq: opened,
    closed_seq: opened + 2,
    status: "complete",
    provenance: AUTHORED,
    text,
    origin: "harness",
  };
}

function asked(text: string): Item {
  const opened = next();
  return {
    kind: "prompt",
    opened_seq: opened,
    closed_seq: opened + 2,
    status: "complete",
    provenance: AUTHORED,
    text,
    origin: "spa",
  };
}

function spoke(text: string, status: Lifecycle = "complete"): Item {
  const opened = next();
  return {
    kind: "message",
    opened_seq: opened,
    closed_seq: status === "open" ? null : opened + 1,
    status,
    provenance: AUTHORED,
    text,
    backend_item_id: null,
  };
}

/** One call, whole: the ask, and the answer where one has arrived. */
function called(
  call_id: string,
  tool_name: string,
  args: Record<string, unknown>,
  answer?: { text: string; outcome: "succeeded" | "failed" }
): Item {
  const opened = next();
  return {
    kind: "tool_call",
    opened_seq: opened,
    closed_seq: answer ? opened + 1 : null,
    status: answer ? "complete" : "open",
    provenance: AUTHORED,
    call_id,
    tool_name,
    arguments: args,
    content: answer?.text ?? "",
    structured: null,
    outcome: answer?.outcome ?? null,
  };
}

/** A thought: dimmed to a line while withheld, folded behind "Thinking" once it has text. */
function thought(text: string): Item {
  const opened = next();
  return {
    kind: "reasoning",
    opened_seq: opened,
    closed_seq: opened + 1,
    status: "complete",
    provenance: AUTHORED,
    text,
    disclosure: text ? "summary" : "withheld",
  };
}

const EDIT_ARGUMENTS = {
  file_path: "/workspace/src/renderer.ts",
  old_string: "const transcript = items.map(renderItem);\n".repeat(16),
  new_string: "const transcript = items.map(renderConversationEntry);\n".repeat(16),
};
const DIFF_CHECK_OUTPUT = Array.from(
  { length: 14 },
  (_unused, line) => `checked generated file ${line + 1}: no whitespace errors`
).join("\n");

const CATALOG_ANSWER =
  '{"servers": [{"server_id": "gmail", "status": "alive"}, {"server_id": "tana", "status": "degraded"}]}';

const boundaryItems: Item[] = [
  asked("Try the Haku Console MCP tools."),
  spoke("I'll start with the catalog, then try a read-only query."),
  called(
    "toolu_01HakuConsoleRead",
    "mcp__haku-console__haku-console__list_mcp_servers",
    {},
    {
      text: CATALOG_ANSWER,
      outcome: "succeeded",
    }
  ),
  called("toolu_01EditTranscript", "Edit", EDIT_ARGUMENTS, {
    text: "Updated /workspace/src/renderer.ts",
    outcome: "succeeded",
  }),
  called(
    "toolu_02BashTranscript",
    "Bash",
    { command: "git diff --check" },
    {
      text: DIFF_CHECK_OUTPUT,
      outcome: "succeeded",
    }
  ),
  spoke("The Haku Console catalog is available. Next I'll try the read-only query."),
];
const standardItems: Item[] = [
  asked("Create a **short note** in the sandbox and tell me what you wrote."),
  spoke(
    "I created `/workspace/note.txt` with:\n\n> Hello from the disposable Haku sandbox.\n\n- Saved locally\n- Ready to inspect"
  ),
];
const overflowingItems: Item[] = Array.from({ length: 8 }, (_unused, index) => [
  asked(`Question **${index + 1}**: inspect the current sandbox state.`),
  spoke(
    index === 7
      ? "### Latest answer\n\nThe transcript stayed pinned to the newest item."
      : `Answer ${index + 1}: the sandbox is **ready**.`
  ),
]).flat();

// The same transcript with every state a tool call can be in: answered, failed, still running, and
// one long enough to need its own scroll — plus a message still being written, which is what
// `status: "open"` renders as.
const toolUsingItems: Item[] = [
  ...standardItems,
  // One of each thinking shape: withheld folds to the one-liner, disclosed to an openable fold.
  thought(""),
  thought(
    "The note should live in the sandbox workspace, so `/workspace` is the right root; a failed write there is worth surfacing verbatim."
  ),
  called(
    "toolu_01HakuConsoleRead",
    "mcp__haku-console__haku-console__list_mcp_servers",
    {},
    {
      text: CATALOG_ANSWER,
      outcome: "succeeded",
    }
  ),
  called(
    "toolu_06BashDescribed",
    "Bash",
    {
      command: 'find / -maxdepth 4 -iname "*haku-state*" 2>/dev/null',
      description: "Search for the haku-state checkout",
    },
    { text: "/workspace/haku-state", outcome: "succeeded" }
  ),
  called(
    "toolu_02WriteNote",
    "Write",
    { file_path: "/workspace/note.txt", content: "Hello from the disposable Haku sandbox." },
    { text: "EACCES: permission denied, open '/workspace/note.txt'", outcome: "failed" }
  ),
  called("toolu_03StillRunning", "Bash", { command: "rg --files | wc -l" }),
  called("toolu_04Edit", "Edit", EDIT_ARGUMENTS, {
    text: "Updated /workspace/src/renderer.ts",
    outcome: "succeeded",
  }),
  called(
    "toolu_05BashOutput",
    "Bash",
    { command: "git diff --check" },
    {
      text: DIFF_CHECK_OUTPUT,
      outcome: "succeeded",
    }
  ),
  woke('Background command "Retry unshallow fetch with longer timeout" completed (exit code 0)'),
  spoke("The background fetch finished — the repo is fully unshallowed now."),
  spoke("Checking whether the fetch left the tree clean", "open"),
];
const conversationId = "70000000-0000-4000-8000-000000000001";
const conversationSessionId = "70000000-0000-4000-8000-000000000011";
// What the shared sandbox bootstrap script writes, forwarded verbatim by the runner — long,
// unbroken paths included, since those are what a narrow viewport has to wrap rather than
// scroll sideways.
const setupNarration = [
  {
    kind: "setup_output",
    frame_seq: 1,
    text: "+ install -m 600 /var/run/secrets/haku/netrc /root/.netrc",
    created_at: "2026-08-01T02:59:41Z",
  },
  {
    kind: "setup_output",
    frame_seq: 2,
    text: "Cloning into '/workspace/haku-state'...",
    created_at: "2026-08-01T02:59:42Z",
  },
  {
    kind: "setup_output",
    frame_seq: 3,
    text: "remote: Enumerating objects: 4821, done.",
    created_at: "2026-08-01T02:59:44Z",
  },
  {
    kind: "setup_output",
    frame_seq: 4,
    text: "Receiving objects: 100% (4821/4821), 12.44 MiB | 8.30 MiB/s, done.",
    created_at: "2026-08-01T02:59:51Z",
  },
  {
    kind: "setup_output",
    frame_seq: 5,
    text: "Resolving deltas: 100% (2610/2610), done.",
    created_at: "2026-08-01T02:59:52Z",
  },
  {
    kind: "setup_output",
    frame_seq: 6,
    text: "Workspace ready at /workspace/haku-state (tip 9f2c1ab8d4e05137c2a9b6f1e83d47a0c5b29e6f).",
    created_at: "2026-08-01T02:59:53Z",
  },
] satisfies Conversation["narration"];
// One conversation per row, with the channels holding it rather than one surface: the first is a
// room with a live session, the second a room between runners whose last session closed cleanly,
// the third a browser thread whose runner failed. A failed session is never live — the backend
// reports it through `last_session_status` — so the third row is the shape production actually
// serves for a failed thread.
const conversationPage = {
  conversations: [
    {
      conversation_id: conversationId,
      harness_kind: "claude_code",
      created_at: "2026-08-01T03:00:00Z",
      last_activity_at: "2026-08-01T03:01:00Z",
      attachments: [{ surface: "matrix", address: "!ops:example.org", attached_at: "2026-08-01T03:00:00Z" }],
      live_session: {
        session_id: conversationSessionId,
        status: "ready",
        error: null,
        created_at: "2026-08-01T03:00:00Z",
        updated_at: "2026-08-01T03:01:00Z",
      },
      last_session_status: null,
      item_count: 6,
    },
    {
      conversation_id: "70000000-0000-4000-8000-0000000000a2",
      harness_kind: "claude_code",
      created_at: "2026-07-31T18:20:00Z",
      last_activity_at: "2026-07-31T18:42:00Z",
      attachments: [{ surface: "matrix", address: "!archive:example.org", attached_at: "2026-07-31T18:20:00Z" }],
      live_session: null,
      last_session_status: "closed",
      item_count: 8,
    },
    {
      conversation_id: "70000000-0000-4000-8000-0000000000a3",
      harness_kind: "claude_code",
      created_at: "2026-07-30T09:10:00Z",
      last_activity_at: "2026-07-30T09:12:00Z",
      attachments: [],
      live_session: null,
      last_session_status: "failed",
      item_count: 2,
    },
  ],
  // Not the last page, so the keyset's "Load older conversations" control renders.
  next_cursor: { last_activity_at: "2026-07-29T22:05:00Z", conversation_id: "70000000-0000-4000-8000-0000000000a4" },
} satisfies ConversationPage;
// Two exchanges; the second's answer was cut off by the runner dying, which is what
// `status: "failed"` renders as.
const conversationItems: Item[] = [
  ...boundaryItems,
  asked("Now check whether the degraded server recovered."),
  { ...spoke("The reflection call timed out before I could ans"), status: "failed" },
];
const conversationSession = {
  session_id: conversationSessionId,
  status: "ready",
  error: null,
  created_at: "2026-08-01T03:00:00Z",
  updated_at: "2026-08-01T03:01:00Z",
} satisfies Conversation["session"];
const conversationDetail = {
  conversation_id: conversationId,
  agent_id: "40000000-0000-4000-8000-000000000004",
  harness_kind: "claude_code",
  created_at: "2026-08-01T03:00:00Z",
  attachments: [{ surface: "matrix", address: "!ops:example.org", attached_at: "2026-08-01T03:00:00Z" }],
  items: conversationItems,
  session: conversationSession,
  provisioning: null,
  narration: setupNarration,
  // The thread ran one session before this one: what a sandbox dying looks like from the
  // conversation's side, and the only place its frame log stays reachable from.
  earlier_sessions: [
    {
      session_id: "70000000-0000-4000-8000-000000000010",
      status: "failed",
      error: "the sandbox pod was evicted",
      created_at: "2026-07-31T22:14:00Z",
      updated_at: "2026-07-31T22:20:00Z",
    },
  ],
} satisfies Conversation;
// The same session a few seconds earlier: still provisioning, mid-clone, with nothing but the
// narration to show.
const conversationBootstrap = {
  ...conversationDetail,
  items: [],
  session: { ...conversationSession, status: "provisioning", updated_at: "2026-08-01T02:59:51Z" },
  narration: setupNarration.slice(0, 4),
} satisfies Conversation;
// A sandbox still being handed out, where the live Kubernetes read is the whole account for a
// session that never comes up.
const conversationProvisioning = {
  ...conversationDetail,
  items: [],
  session: { ...conversationSession, status: "provisioning", updated_at: "2026-08-01T03:00:03Z" },
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
} satisfies Conversation;
// A finished session short enough that the collapsed panel stays on screen: the detail scene's
// transcript opens scrolled to its newest message, which puts the collapsed panel above the fold.
const conversationNarrationCollapsed = {
  ...conversationDetail,
  items: standardItems,
} satisfies Conversation;
// A transcript long enough to overflow its viewport, so the scroll stays pinned to the newest
// message rather than opening at the top.
const conversationOverflow = {
  ...conversationDetail,
  items: overflowingItems,
  narration: [],
} satisfies Conversation;
// Tool calls with their results, which is what the transcript's card rendering exists for — with
// a message still being written at the tail.
const conversationToolUse = {
  ...conversationDetail,
  items: toolUsingItems,
  narration: [],
} satisfies Conversation;
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
const claudeFrame = (payload: Record<string, unknown>) => payload;

const conversationFrames = {
  conversation_id: conversationId,
  harness_kind: "claude_code",
  frames: [
    {
      frame_seq: 412,
      direction: "to_agent",
      kind: "harness_frame",
      created_at: "2026-08-01T03:00:20Z",
      payload: claudeFrame({
        type: "user",
        message: { role: "user", content: "Now check whether the degraded server recovered." },
      }),
    },
    {
      frame_seq: 413,
      direction: "from_agent",
      kind: "harness_frame",
      created_at: "2026-08-01T03:00:21Z",
      payload: claudeFrame({
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
      }),
    },
    {
      frame_seq: 414,
      direction: "from_agent",
      kind: "harness_frame",
      created_at: "2026-08-01T03:00:23Z",
      payload: claudeFrame({
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
      }),
    },
    {
      frame_seq: 416,
      direction: "from_agent",
      kind: "harness_frame",
      created_at: "2026-08-01T03:00:24Z",
      payload: claudeFrame({
        type: "result",
        subtype: "error_during_execution",
        is_error: true,
        duration_ms: 3600,
        total_cost_usd: 0.0041,
        usage: { input_tokens: 1900, output_tokens: 60 },
      }),
    },
  ],
  next_before_seq: 412,
} satisfies SessionFramePage;
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
  // What the conversations pages launch a Web chat with; the harness's own iframe origin.
  if (url.includes("/api/config")) {
    return jsonResponse({
      launch_routine_url: null,
      haku_ui_url: "https://haku-ui.test",
      // Two options so the list header renders its runtime picker (shown only for >1 option) —
      // the picker + button is the launcher the narrow-viewport scene exercises for overflow.
      chat_launch_options: [
        {
          agent_id: "40000000-0000-4000-8000-000000000004",
          agent_display_name: "Haku",
          runtime: "claude_code",
          runtime_display_name: "Claude Code",
        },
        {
          agent_id: "40000000-0000-4000-8000-000000000005",
          agent_display_name: "Public coder agent",
          runtime: "codex_app_server",
          runtime_display_name: "Codex",
        },
      ],
    });
  }
  if (url.includes("/api/deployment")) return jsonResponse(SAMPLE_DEPLOYMENT);
  // Before the conversation detail below, which its path is a prefix of.
  if (url.includes("/frames")) return jsonResponse(conversationFrames);
  // The refusal the composer has to render: `enqueue_prompt` answers 409 and records nothing, so
  // the operator's text has to survive it. Before the conversation detail below, whose path this
  // now extends.
  if (scene === "conversation-prompt-refused" && url.includes("/messages")) {
    return new Response(JSON.stringify({ detail: "a prompt is already queued" }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (url.includes("/api/conversations/")) return jsonResponse(conversationDetailForScene);
  if (url.includes("/api/conversations")) return jsonResponse(conversationPage);
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
    haku_index__index_status: () => SAMPLE_INDEX_STATUS,
    haku_session_sandboxes__list_active: () => ({ items: SAMPLE_ACTIVE_SANDBOXES, next_cursor: null }),
    haku_session_sandboxes__terminate: (args) => ({ session_id: String(args.session_id), status: "terminated" }),
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
