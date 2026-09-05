// Deterministic sample data for the screenshot scenes (harness.tsx) and the API stub
// (mock_api.ts). Kept separate so both share one source of truth.
import { makeRecentToolCall, type RecentToolCall } from "../approval_state";
import type { AiquotaView, DeploymentInfo, GrantListResponse, ToolCallRecord } from "../client";
import type { DaemonStatus, IndexStatus, McpServerConnection, McpServerProbe } from "../mcp_status_client";
import type { RegisteredToolPreviewFixture } from "../tool_rendering/index";

export const SAMPLE_ACTIVE_SANDBOXES = [
  {
    session_id: "70000000-0000-4000-8000-000000000021",
    harness_kind: "claude_code",
    status: "provisioning",
    created_at: "2026-08-01T03:02:00Z",
    updated_at: "2026-08-01T03:02:12Z",
    sandbox: {
      step: "waiting_for_pod_ready",
      inspected_at: "2026-08-01T03:02:12Z",
      claim_name: "claude-70000000000040008000000000000021",
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
  },
  {
    session_id: "70000000-0000-4000-8000-000000000022",
    harness_kind: "codex_app_server",
    status: "responding",
    created_at: "2026-08-01T03:01:00Z",
    updated_at: "2026-08-01T03:02:14Z",
    sandbox: {
      step: "waiting_for_runner",
      inspected_at: "2026-08-01T03:02:14Z",
      claim_name: "codex-70000000000040008000000000000022",
      claim_ready: true,
      claim_reason: "Ready",
      claim_message: "Sandbox is ready",
      sandbox_name: "haku-codex-2f6ab",
      sandbox_ready: true,
      pod_name: "haku-codex-2f6ab",
      pod_phase: "Running",
      pod_ready: true,
      runner_ready: true,
      runner_state: "ready",
      observation_error: null,
    },
  },
  {
    session_id: "70000000-0000-4000-8000-000000000023",
    harness_kind: "claude_code",
    status: "closing",
    created_at: "2026-08-01T02:55:00Z",
    updated_at: "2026-08-01T03:02:16Z",
    sandbox: {
      step: "waiting_for_runner",
      inspected_at: "2026-08-01T03:02:16Z",
      claim_name: "claude-70000000000040008000000000000023",
      claim_ready: true,
      claim_reason: "Ready",
      claim_message: "Sandbox is ready",
      sandbox_name: "haku-claude-1ba2c",
      sandbox_ready: true,
      pod_name: "haku-claude-1ba2c",
      pod_phase: "Running",
      pod_ready: true,
      runner_ready: true,
      runner_state: "ready",
      observation_error: null,
    },
  },
] as const;

const STOCK_ADD_HISTORY_FIXTURE = {
  serverId: "grocy-sf",
  toolName: "stock_add",
  args: { items: [{ product: "Rolled oats", amount: 2, qu: "pack", location: "Pantry" }] },
} satisfies RegisteredToolPreviewFixture;

const CALENDAR_HISTORY_FIXTURE = {
  serverId: "google_calendar",
  toolName: "create_event",
  args: {
    summary: "Dentist",
    start: { date_time: "2026-07-12T09:00:00", time_zone: "America/Los_Angeles" },
    end: { date_time: "2026-07-12T10:00:00", time_zone: "America/Los_Angeles" },
    recurrence: ["RRULE:FREQ=MONTHLY;COUNT=3"],
  },
} satisfies RegisteredToolPreviewFixture;

const KUBECTL_HISTORY_FIXTURE = {
  serverId: "kubectl-passthrough-mcp",
  toolName: "pods_delete",
  args: { namespace: "haku-sandbox", name: "worker-6f9c2" },
} satisfies RegisteredToolPreviewFixture;

const STOCK_ADD_PENDING_FIXTURE = {
  serverId: "grocy-sf",
  toolName: "stock_add",
  args: {
    items: [
      { product: "Rolled oats", amount: 2, qu: "pack", location: "Pantry" },
      { product: "Almond butter", amount: 1, qu: "jar", location: "Pantry" },
      { product: "Frozen berries", amount: 3, qu: "bag", location: "Freezer" },
      { product: "Oat milk", amount: 6, qu: "carton", location: "Fridge" },
      { product: "Dark chocolate", amount: 4, qu: "bar", location: "Pantry" },
    ],
  },
} satisfies RegisteredToolPreviewFixture;

type StoredToolResult = NonNullable<ToolCallRecord["result"]>;

// The stored wire shape of an executed call's result (mcp/approval.py's `_mcp_result_to_json`):
// FastMCP dumps the return into a JSON text block + structuredContent, wrapping a non-dict
// return (a list, a scalar) as `{"result": …}` with the wrap flagged in `_meta`.
function callToolResult(value: unknown): StoredToolResult {
  const wrap = typeof value !== "object" || value === null || Array.isArray(value);
  return {
    content: [{ type: "text", text: JSON.stringify(value) }],
    isError: false,
    structuredContent: wrap ? { result: value } : value,
    ...(wrap ? { _meta: { fastmcp: { wrap_result: true } } } : {}),
  };
}

function toolCall(overrides: Partial<ToolCallRecord> & Pick<ToolCallRecord, "tool_call_id">): ToolCallRecord {
  return {
    server_id: STOCK_ADD_HISTORY_FIXTURE.serverId,
    tool_name: STOCK_ADD_HISTORY_FIXTURE.toolName,
    caller: {
      kind: "agent",
      agent_id: "11111111-1111-4111-8111-111111111111",
      display_name: "Haku agent",
    },
    status: "ok",
    created_at: "2026-07-09T14:32:00Z",
    updated_at: "2026-07-09T14:32:04Z",
    arguments: STOCK_ADD_HISTORY_FIXTURE.args,
    rationale: "Thrive box delivered; adding its items to inventory.",
    title: null,
    result: callToolResult([
      {
        kind: "ok",
        product_name: "Rolled oats",
        transaction_id: "6f0b2c9e",
        amount_delta: 2,
        new_amount: 5,
        qu_name: "Pack",
        stock_qu_name: null,
        location_name: "Pantry",
        entry_id: 189,
        best_before_date: "2026-12-01",
      },
    ]),
    error: null,
    decision_note: null,
    decision_operator_id: null,
    withdrawal_reason: null,
    ...overrides,
  };
}

export const SAMPLE_TOOL_CALLS: ToolCallRecord[] = [
  toolCall({
    tool_call_id: "tc_0",
    server_id: "grocy-sf",
    tool_name: "shopping_list_items_remove",
    title: "Remove bought items from the weekly list",
    status: "pending_approval",
    rationale: "These are already in stock after the Thrive delivery, so drop them from the list.",
    result: null,
    arguments: { ids: [3, 7, 12] },
    // The history scene toggles this first row to "Full", so it shows the declined-policy
    // evaluation; tc_3 below carries the same kind of string on a compact row, where it is hidden.
    auto_approval_evaluation: "manual: grocy-sf/shopping_list_items_remove is not a read-only tool",
  }),
  toolCall({ tool_call_id: "tc_1", title: "Add Thrive box items to Grocy", status: "ok" }),
  toolCall({
    tool_call_id: "tc_2",
    server_id: CALENDAR_HISTORY_FIXTURE.serverId,
    tool_name: CALENDAR_HISTORY_FIXTURE.toolName,
    title: "Create calendar event: Dentist",
    status: "error",
    rationale: "Booked a dentist appointment from the confirmation email.",
    error: "Calendar API returned 403: insufficient scope for calendar.events.",
    result: null,
    caller: { kind: "operator" },
    arguments: CALENDAR_HISTORY_FIXTURE.args,
  }),
  toolCall({
    tool_call_id: "tc_3",
    server_id: KUBECTL_HISTORY_FIXTURE.serverId,
    tool_name: KUBECTL_HISTORY_FIXTURE.toolName,
    title: "Delete crashlooping pod",
    status: "denied",
    rationale: "The worker pod has been CrashLoopBackOff for 20 minutes; restart it.",
    decision_note: "Not without a rollout plan — investigate the crash first.",
    decision_operator_id: "00000000-0000-4000-8000-000000000001",
    auto_approval_evaluation: "manual: kubectl/delete is never auto-approved",
    result: null,
    caller: { kind: "operator" },
    arguments: KUBECTL_HISTORY_FIXTURE.args,
  }),
  // Retracted by the submitting agent before anyone decided it — the third exit from the queue.
  toolCall({
    tool_call_id: "tc_5",
    server_id: "grocy-sf",
    tool_name: "shopping_list_items_add",
    title: "Add oat milk to the weekly list",
    status: "withdrawn",
    rationale: "Running low on oat milk after the weekend.",
    withdrawal_reason: "Superseded by tc_9, which adds the whole Thrive reorder in one call.",
    result: null,
    arguments: { shopping_list_id: 1, product_id: 42, amount: 2 },
  }),
  // Unconditionally auto-approved (no operator decision) — exercises the history view's
  // "Show auto-approved" filter, which hides this row by default.
  toolCall({
    tool_call_id: "tc_4",
    server_id: "gmail",
    tool_name: "labels_list",
    title: "List Gmail labels",
    status: "ok",
    rationale: "Checking for an existing haku/ label before filing this thread.",
    result: callToolResult({ labels: [{ id: "Label_1", name: "haku/receipts" }] }),
    approval_policy_id: "unconditional_v1",
    auto_approval_evaluation: "approved: gmail/labels_list is allowlisted read-only/safe",
    arguments: {},
  }),
];

export const SAMPLE_PENDING: ToolCallRecord[] = [
  toolCall({
    tool_call_id: "tc_p1",
    server_id: "grocy-sf",
    tool_name: "shopping_list_items_remove",
    title: "Remove bought items from the weekly list",
    caller: {
      kind: "agent",
      agent_id: "11111111-1111-4111-8111-111111111111",
      display_name: "Haku agent",
    },
    rationale: "These are already in stock after the Thrive delivery, so drop them from the list.",
    arguments: { ids: [3, 7, 12] },
    created_at: "2026-07-09T14:40:00Z",
    updated_at: "2026-07-09T14:40:00Z",
    status: "pending_approval",
    result: null,
  }),
  toolCall({
    // A many-item grocy stock_add — exercises the compact widget's "first few + … +N more".
    tool_call_id: "tc_p2",
    server_id: STOCK_ADD_PENDING_FIXTURE.serverId,
    tool_name: STOCK_ADD_PENDING_FIXTURE.toolName,
    title: "Add Thrive box items to Grocy",
    caller: {
      kind: "agent",
      agent_id: "11111111-1111-4111-8111-111111111111",
      display_name: "Haku agent",
    },
    rationale: "Thrive box delivered; adding its items to inventory.",
    arguments: STOCK_ADD_PENDING_FIXTURE.args,
    created_at: "2026-07-09T14:41:00Z",
    updated_at: "2026-07-09T14:41:00Z",
    status: "pending_approval",
    result: null,
  }),
];

export function sampleRecentToolCalls(nowMs: number): RecentToolCall[] {
  const record = toolCall({ tool_call_id: "tc_r1", title: "Add Thrive box items to Grocy", status: "ok" });
  const recent = makeRecentToolCall(record, nowMs);
  if (!recent) throw new Error(`Expected terminal screenshot fixture, got ${record.status}`);
  return [recent];
}

export const SAMPLE_MCP_SERVERS: McpServerConnection[] = [
  {
    server_id: "grocy-sf",
    backend: {
      kind: "remote_mcp",
      url: "https://grocy-sf.example.test/mcp",
      auth: { kind: "remote_server_oauth", client_registration: { kind: "dynamic", client_name: "Haku Console" } },
    },
    connection: {
      server_id: "grocy-sf",
      username: "agentydragon",
      state: {
        status: "degraded",
        connected_at: "2026-07-01T09:00:00Z",
        token_expires_at: "2026-07-17T10:00:00Z",
        scope: "read write",
        refresh_failure: {
          started_at: "2026-07-17T09:59:00Z",
          initial: {
            at: "2026-07-17T09:59:00Z",
            kind: "outcome_unknown",
            message: "MCP OAuth token refresh timed out after 30 seconds",
          },
          latest: {
            at: "2026-07-17T09:59:00Z",
            kind: "outcome_unknown",
            message: "MCP OAuth token refresh timed out after 30 seconds",
          },
          attempts: 1,
          resolution: "Reconnect the account before retrying.",
          next_retry_at: null,
        },
      },
    },
  },
  {
    server_id: "tana-rw",
    backend: {
      kind: "remote_mcp",
      url: "http://tana-mcp.tana-mcp.svc.cluster.local:8263/mcp",
      auth: { kind: "static_bearer" },
    },
    connection: null,
  },
  {
    server_id: "gmail",
    backend: {
      kind: "in_process",
      credential: { kind: "operator_connection", connection: "google_mail" },
    },
    connection: {
      connection: "google_mail",
      display_name: "Google Mail",
      provider: "google",
      status: "connected",
      connected_at: "2026-07-01T09:00:00Z",
      token_expires_at: "2026-08-17T10:00:00Z",
      scope: "https://www.googleapis.com/auth/gmail.modify",
    },
  },
  {
    server_id: "google_calendar",
    backend: {
      kind: "in_process",
      credential: { kind: "operator_connection", connection: "google_calendar" },
    },
    connection: {
      connection: "google_calendar",
      display_name: "Google Calendar",
      provider: "google",
      status: "unprovisioned",
      detail: "OAuth client not provisioned on this console; see the console deployment README.",
    },
  },
];

export const SAMPLE_MCP_PROBES: Record<string, McpServerProbe> = Object.fromEntries(
  SAMPLE_MCP_SERVERS.map((connection) => [
    connection.server_id,
    {
      connection,
      server:
        connection.server_id === "grocy-sf"
          ? {
              server_id: connection.server_id,
              title: connection.server_id,
              state: {
                status: "degraded" as const,
                failure_stage: "credential_resolution" as const,
                degraded_reason: "MCP OAuth token refresh failed: 401",
              },
            }
          : connection.connection?.status === "unprovisioned"
            ? {
                server_id: connection.server_id,
                title: connection.server_id,
                state: {
                  status: "degraded" as const,
                  failure_stage: "credential_resolution" as const,
                  degraded_reason:
                    "OAuth client for google_calendar is not provisioned on this console; see the console deployment README.",
                },
              }
            : {
                server_id: connection.server_id,
                title: connection.server_id,
                state: { status: "alive" as const, tools: [] },
              },
    },
  ])
);

export const SAMPLE_GRANTS: GrantListResponse = {
  grants: [
    {
      source: {
        kind: "database",
        id: "50000000-0000-4000-8000-000000000005",
        tool_call_id: "tc_0123456789abcdef01234567",
        created_at: "2025-02-01T11:35:00Z",
      },
      subject: { kind: "agent", agent_id: "30000000-0000-4000-8000-000000000003" },
      coverage: {
        kind: "kubernetes_rules",
        scope: { kind: "namespaces", namespaces: ["public-coder-agent"] },
        rules: [
          {
            api_groups: [""],
            resources: ["pods/log"],
            verbs: ["get"],
            resource_names: [],
            non_resource_urls: [],
          },
          {
            api_groups: ["apps"],
            resources: ["deployments"],
            verbs: ["get", "list"],
            resource_names: [],
            non_resource_urls: [],
          },
        ],
      },
      validity: { ends_at: "2025-02-02T02:05:00Z", status: "active", ended_at: null, end_reason: null },
    },
    {
      source: {
        kind: "database",
        id: "50000000-0000-4000-8000-000000000007",
        tool_call_id: "tc_0123456789abcdef01234567",
        created_at: "2025-02-01T11:35:00Z",
      },
      subject: { kind: "agent", agent_id: "30000000-0000-4000-8000-000000000003" },
      coverage: {
        kind: "kubernetes_rules",
        scope: { kind: "cluster" },
        rules: [
          {
            api_groups: ["rbac.authorization.k8s.io"],
            resources: ["clusterroles"],
            verbs: ["get", "list"],
            resource_names: [],
            non_resource_urls: [],
          },
        ],
      },
      validity: { ends_at: "2025-02-02T02:05:00Z", status: "active", ended_at: null, end_reason: null },
    },
    {
      source: {
        kind: "database",
        id: "50000000-0000-4000-8000-000000000006",
        tool_call_id: "tc_1123456789abcdef01234567",
        created_at: "2025-01-31T21:00:00Z",
      },
      subject: { kind: "session", session_id: "60000000-0000-4000-8000-000000000006" },
      coverage: {
        kind: "kubernetes_rules",
        scope: { kind: "cluster" },
        rules: [
          {
            api_groups: [""],
            resources: ["nodes"],
            verbs: ["get"],
            resource_names: ["wyrm2"],
            non_resource_urls: [],
          },
        ],
      },
      validity: {
        ends_at: "2025-01-31T22:00:00Z",
        status: "ended",
        ended_at: "2025-01-31T21:20:00Z",
        end_reason: "Pilot complete; return to standard diagnostics.",
      },
    },
    {
      source: { kind: "config_file", entry_id: "grocy-read" },
      subject: { kind: "access_profile", access_profile_id: "public-coder" },
      coverage: {
        kind: "http",
        origins: [{ scheme: "https", host: "grocy.example", port: 443 }],
        coverage: { methods: ["GET"], path_regex: "/api/.*" },
        credential_handles: ["grocy-readonly"],
        allow_prohibited_address: false,
      },
      validity: { ends_at: null, status: "active", ended_at: null, end_reason: null },
    },
  ],
} satisfies GrantListResponse;

export const SAMPLE_DAEMONS: DaemonStatus[] = [
  {
    daemon_id: "wyrm2",
    display_name: "wyrm2",
    status: "busy",
    last_heartbeat_at: "2025-02-01T11:58:00Z",
    version: "0.1.0",
    backends: ["hostexec"],
    active_execution_id: "8c8b5bc2-8b0c-4e89-9f1b-8129fa28d255",
  },
  {
    daemon_id: "rugged",
    display_name: "rugged",
    status: "offline",
    last_heartbeat_at: "2025-01-31T14:10:00Z",
    version: "0.1.0",
    backends: ["hostexec"],
    active_execution_id: null,
  },
];

export const SAMPLE_DEPLOYMENT: DeploymentInfo = {
  server: {
    image_tag: "devel-20260713014452-83da566",
    source_commit: "83da566",
    source_commit_url: "https://github.com/agentydragon/ducktape/commit/83da566",
  },
  frontend: {
    image_tag: "devel-20260713015518-bfad4bf",
    source_commit: "bfad4bf",
    source_commit_url: "https://github.com/agentydragon/ducktape/commit/bfad4bf",
  },
};

export const SAMPLE_INDEX_STATUS: IndexStatus = {
  indexes: [
    {
      index_type: "git",
      index_id: "ducktape",
      indexed_commit: "83da566ac718a9ef",
      remote_commit: "bfad4bf03a91b80c",
      remote_seen_at: "2026-07-20T19:34:00Z",
      branch: "devel",
      indexed_at: "2026-07-20T19:30:00Z",
      files: 1842,
      chunks: 9350,
      embedded_chunks: 9350,
      pending_chunks: 0,
      superseded_chunks: 212,
    },
    {
      index_type: "git",
      index_id: "haku-state",
      indexed_commit: "5eb73c778214a9ef",
      remote_commit: "5eb73c778214a9ef",
      remote_seen_at: "2026-07-20T19:34:00Z",
      branch: "main",
      indexed_at: "2026-07-20T19:33:00Z",
      files: 96,
      chunks: 481,
      embedded_chunks: 481,
      pending_chunks: 0,
      superseded_chunks: 0,
    },
    {
      index_type: "chat",
      index_id: "console-chats",
      sessions: 128,
      chunks: 774,
      stale_sessions: 0,
      unindexed_messages: 0,
      lag_seconds: null,
      last_indexed_at: "2026-07-20T19:33:30Z",
      embedded_chunks: 774,
      pending_chunks: 0,
      superseded_chunks: 34,
    },
  ],
};

export const SAMPLE_AIQUOTA: AiquotaView = {
  fetched_at: "2026-08-30T17:05:00Z",
  providers: [
    {
      provider: "Anthropic",
      last_output: {
        fetched_at: "2026-08-30T17:05:00Z",
        result: {
          kind: "success",
          windows: [
            {
              name: "5-hour",
              display: true,
              used_percent: 61,
              reset_seconds: 10_800,
              window_seconds: 18_000,
              reset_at: "2026-08-30T20:05:00Z",
            },
            {
              name: "7-day",
              display: true,
              used_percent: 34,
              reset_seconds: 345_600,
              window_seconds: 604_800,
              reset_at: "2026-09-04T17:05:00Z",
            },
          ],
          extra_spend: { is_enabled: true, monthly_limit_usd: 100, used_usd: 18.42, utilization: 0.1842 },
        },
      },
      last_success: null,
      currently_over_plan: false,
      extra_status: "informational",
      burn: null,
    },
    {
      provider: "OpenAI",
      last_output: {
        fetched_at: "2026-08-30T17:04:00Z",
        result: {
          kind: "success",
          windows: [
            {
              name: "3-hour",
              display: true,
              used_percent: 87,
              reset_seconds: 5_400,
              window_seconds: 10_800,
              reset_at: "2026-08-30T20:04:00Z",
            },
          ],
          extra_spend: null,
          available_reset_credits: 2,
          available_reset_credit_expiries: ["2026-09-20T09:00:00Z"],
        },
      },
      last_success: null,
      currently_over_plan: false,
      extra_status: "none",
      burn: null,
    },
  ],
};
