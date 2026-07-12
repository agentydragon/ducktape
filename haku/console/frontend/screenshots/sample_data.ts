// Deterministic sample data for the screenshot scenes (harness.tsx) and the API stub
// (mock_api.ts). Kept separate so both share one source of truth.
import { makeRecentToolCall, type RecentToolCall } from "../approval_state.ts";
import type { McpOperatorAuthStatus, PendingApproval, ToolCallRecord } from "../client.ts";
import type { RegisteredToolPreviewFixture } from "../tool_previews/index.tsx";

const STOCK_ADD_HISTORY_FIXTURE = {
  serverId: "grocy-sf",
  toolName: "stock_add",
  args: { items: [{ product: "Rolled oats", amount: 2, qu: "pack", location: "Pantry" }] },
} satisfies RegisteredToolPreviewFixture;

const CALENDAR_HISTORY_FIXTURE = {
  serverId: "google_calendar",
  toolName: "create_calendar_event",
  args: {
    summary: "Dentist",
    start: { date_time: "2026-07-12T09:00:00", time_zone: "America/Los_Angeles" },
    end: { date_time: "2026-07-12T10:00:00", time_zone: "America/Los_Angeles" },
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

function toolCall(overrides: Partial<ToolCallRecord> & Pick<ToolCallRecord, "tool_call_id">): ToolCallRecord {
  return {
    server_id: STOCK_ADD_HISTORY_FIXTURE.serverId,
    tool_name: STOCK_ADD_HISTORY_FIXTURE.toolName,
    caller_principal: "haku-agent-api-token",
    status: "ok",
    created_at: "2026-07-09T14:32:00Z",
    updated_at: "2026-07-09T14:32:04Z",
    arguments: STOCK_ADD_HISTORY_FIXTURE.args,
    rationale: "Thrive box delivered; adding its items to inventory.",
    title: null,
    result: { content: [{ type: "text", text: "stock_add:42:2" }], isError: false },
    error: null,
    denial_reason: null,
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
    caller_principal: "operator",
    arguments: CALENDAR_HISTORY_FIXTURE.args,
  }),
  toolCall({
    tool_call_id: "tc_3",
    server_id: KUBECTL_HISTORY_FIXTURE.serverId,
    tool_name: KUBECTL_HISTORY_FIXTURE.toolName,
    title: "Delete crashlooping pod",
    status: "denied",
    rationale: "The worker pod has been CrashLoopBackOff for 20 minutes; restart it.",
    denial_reason: "Not without a rollout plan — investigate the crash first.",
    result: null,
    caller_principal: "operator",
    arguments: KUBECTL_HISTORY_FIXTURE.args,
  }),
];

export const SAMPLE_PENDING: PendingApproval[] = [
  {
    tool_call_id: "tc_p1",
    server_id: "grocy-sf",
    tool_name: "shopping_list_items_remove",
    title: "Remove bought items from the weekly list",
    caller_principal: "haku-agent-api-token",
    rationale: "These are already in stock after the Thrive delivery, so drop them from the list.",
    arguments: { ids: [3, 7, 12] },
    created_at: "2026-07-09T14:40:00Z",
  },
  {
    // A many-item grocy stock_add — exercises the compact widget's "first few + … +N more".
    tool_call_id: "tc_p2",
    server_id: STOCK_ADD_PENDING_FIXTURE.serverId,
    tool_name: STOCK_ADD_PENDING_FIXTURE.toolName,
    title: "Add Thrive box items to Grocy",
    caller_principal: "haku-agent-api-token",
    rationale: "Thrive box delivered; adding its items to inventory.",
    arguments: STOCK_ADD_PENDING_FIXTURE.args,
    created_at: "2026-07-09T14:41:00Z",
  },
];

export function sampleRecentToolCalls(nowMs: number): RecentToolCall[] {
  const record = toolCall({ tool_call_id: "tc_r1", title: "Add Thrive box items to Grocy", status: "ok" });
  const recent = makeRecentToolCall(record, nowMs);
  if (!recent) throw new Error(`Expected terminal screenshot fixture, got ${record.status}`);
  return [recent];
}

export const SAMPLE_MCP: McpOperatorAuthStatus[] = [
  {
    server_id: "grocy-sf",
    status: "connected",
    operator_principal: "agentydragon",
    connected_at: "2026-07-01T09:00:00Z",
    token_expires_at: "2026-08-01T09:00:00Z",
    scope: "read write",
  },
];

// Subject/label lookups the Gmail thread-labels widget fetches; served by mock_api so both
// preview variants render real subjects (compact shows the first few, detailed adds labels).
export const SAMPLE_GMAIL_THREADS = {
  t1: {
    subject: "Q3 planning — notes + open questions",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t1",
    current_label_names: ["Inbox", "Work"],
  },
  t2: {
    subject: "Re: dentist appointment confirmation",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t2",
    current_label_names: ["Inbox"],
  },
  t3: {
    subject: "Your Thrive Market order shipped",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t3",
    current_label_names: ["Inbox", "Receipts"],
  },
  t4: {
    subject: "This week in your neighborhood",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t4",
    current_label_names: ["Inbox", "Newsletters"],
  },
};

// The calendar-name lookup the create-event widget fetches for a non-primary calendar_id;
// served by mock_api so the detailed preview renders the name (linked) instead of the raw id.
export const SAMPLE_CALENDAR_SUMMARY = {
  calendar_id: "family@group.calendar.google.com",
  summary: "Family",
  html_link: "https://calendar.google.com/calendar/u/0/r?cid=ZmFtaWx5QGdyb3VwLmNhbGVuZGFyLmdvb2dsZS5jb20",
};

type PreviewSample = {
  title: string;
  serverId: string;
  toolName: string;
  args: Record<string, unknown>;
};

// Every implemented tool-call preview, for the `previews` gallery scene (harness.tsx renders
// each in both compact and detailed). The registered entries are a discriminated union derived
// from the registry's actual Zod schemas, so a stale id or argument is a type error. The final
// entry intentionally has no widget and exercises the raw-JSON fallback.
const CUSTOM_PREVIEW_SAMPLES = [
  {
    title: "Add Thrive box items to stock",
    serverId: "grocy-sf",
    toolName: "stock_add",
    args: {
      items: [
        { product: "Rolled oats", amount: 2, qu: "pack", location: "Pantry", best_before_date: "2026-12-01" },
        { product: "Almond butter", amount: 1, qu: "jar", location: "Pantry" },
        { product: "Frozen berries", amount: 3, qu: "bag", location: "Freezer" },
        { product: "Oat milk", amount: 6, qu: "carton", location: "Fridge" },
        { product: "Dark chocolate", amount: 4, qu: "bar", location: "Pantry" },
      ],
    },
  },
  {
    title: "Consume spoiled and used groceries",
    serverId: "grocy-sf",
    toolName: "stock_consume",
    args: {
      items: [
        { product: "Milk", amount: 1, qu: "carton", location: "Fridge", spoiled: true },
        { product: "Spinach", amount: 200, qu: "gram", location: "Fridge" },
      ],
    },
  },
  {
    title: "Create pantry products",
    serverId: "grocy-sf",
    toolName: "products_create",
    args: {
      items: [
        {
          name: "Rolled oats",
          stock_qu: "gram",
          location: "Pantry",
          default_best_before_days: 270,
          min_stock_amount: 500,
          product_group: "Grains",
          description: "Organic thick-cut oats.",
        },
        { name: "Almond butter", stock_qu: "jar", location: "Pantry", default_best_before_days: 180 },
      ],
    },
  },
  {
    title: "Update pantry product settings",
    serverId: "grocy-sf",
    toolName: "products_edit",
    args: {
      items: [
        {
          product: "Rolled oats",
          location: "Pantry",
          min_stock_amount: 500,
          default_best_before_days: 270,
          product_group: "Grains",
          clear_fields: ["description"],
        },
        { product: "Almond butter", purchase_qu: "jar", consume_qu: "jar" },
      ],
    },
  },
  {
    title: "Schedule dentist appointment",
    serverId: "google_calendar",
    toolName: "create_calendar_event",
    args: {
      summary: "Dentist appointment",
      start: { date_time: "2026-07-12T09:00:00", time_zone: "America/Los_Angeles" },
      end: { date_time: "2026-07-12T10:00:00", time_zone: "America/Los_Angeles" },
      location: "123 Market St, San Francisco",
      description: "Routine cleaning and checkup.",
      calendar_id: "family@group.calendar.google.com",
      reminders: [{ method: "popup", minutes_before_start: 30 }],
      attendees: ["dentist@example.com"],
    },
  },
  {
    title: "File planning threads for follow-up",
    serverId: "gmail",
    toolName: "threads_modify_labels",
    args: { thread_ids: ["t1", "t2", "t3", "t4"], add: ["Follow up"], remove: ["Inbox"] },
  },
  {
    title: "Draft Q3 planning reply",
    serverId: "gmail",
    toolName: "drafts_create",
    args: {
      to: ["ops@allegedly.works"],
      cc: ["rai@allegedly.works"],
      subject: "Re: Q3 planning",
      body: "Hi team,\n\nThanks for the notes. A few thoughts on the roadmap:\n- Ship the console settings page\n- Then the previews gallery\n- Circle back on datetime formatting\n\nBest,\nRai",
      thread_id: "thread-42",
    },
  },
  {
    title: "Review inbox for replies",
    serverId: "haku_routine",
    toolName: "launch_routine",
    args: { text: "Scan Gmail for anything needing a reply, draft responses, and flag time-sensitive items." },
  },
  {
    title: "Deploy the worker service",
    serverId: "kubectl-passthrough-mcp",
    toolName: "resources_create_or_update",
    args: {
      resource:
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: worker\n  namespace: haku-sandbox\nspec:\n  replicas: 3\n  selector:\n    matchLabels:\n      app: worker\n  template:\n    metadata:\n      labels:\n        app: worker\n    spec:\n      containers:\n        - name: worker\n          image: ghcr.io/agentydragon/worker:latest",
    },
  },
  {
    title: "Delete the failed worker pod",
    serverId: "kubectl-passthrough-mcp",
    toolName: "resources_delete",
    args: { apiVersion: "v1", kind: "Pod", name: "worker-6f9c2", namespace: "haku-sandbox", gracePeriodSeconds: 0 },
  },
  {
    title: "Restart the failed worker pod",
    serverId: "kubectl-passthrough-mcp",
    toolName: "pods_delete",
    args: { name: "worker-6f9c2", namespace: "haku-sandbox" },
  },
  {
    title: "Add planning review tasks to Tana",
    serverId: "tana-rw",
    toolName: "import_tana_paste",
    args: {
      parentNodeId: "inbox",
      content: "- Prepare planning review\n  - Gather Q3 notes\n  - Draft agenda\n  - Confirm attendees",
    },
  },
  {
    title: "Open today's calendar node",
    serverId: "tana-rw",
    toolName: "get_or_create_calendar_node",
    args: { workspaceId: "workspace", granularity: "day", date: "2026-07-11" },
  },
  {
    title: "Trash the obsolete task",
    serverId: "tana-rw",
    toolName: "trash_node",
    args: { nodeId: "task" },
  },
  {
    title: "Rename the quarterly task",
    serverId: "tana-rw",
    toolName: "edit_node",
    args: { nodeId: "task", name: { old_string: "Quarterly", new_string: "Q3", replace_all: false } },
  },
  {
    title: "Move the task into its project",
    serverId: "tana-rw",
    toolName: "move_node",
    args: {
      nodeId: "task",
      targetNodeId: "project",
      sourceParentId: "old-parent",
      position: "end",
      keepSourceReference: true,
    },
  },
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

const FALLBACK_PREVIEW_SAMPLE: PreviewSample = {
  // No widget for this (server, tool) — the generic raw-JSON fallback (compact clamps).
  title: "Remove purchased shopping-list items",
  serverId: "grocy-sf",
  toolName: "shopping_list_items_remove",
  args: { ids: [3, 7, 12, 15, 21, 34, 42, 55] },
};

export const PREVIEW_SAMPLES: PreviewSample[] = [...CUSTOM_PREVIEW_SAMPLES, FALLBACK_PREVIEW_SAMPLE];
