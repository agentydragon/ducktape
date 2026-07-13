// Deterministic sample data for the screenshot scenes (harness.tsx) and the API stub
// (mock_api.ts). Kept separate so both share one source of truth.
import { makeRecentToolCall, type RecentToolCall } from "../approval_state.ts";
import type { DeploymentInfo, McpOperatorAuthStatus, PendingApproval, ToolCallRecord } from "../client.ts";
import type { GrocyReferenceResponse } from "../grocy_client.ts";
import type { RegisteredToolPreviewFixture } from "../tool_rendering/index.tsx";

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

type StoredToolResult = NonNullable<ToolCallRecord["result"]>;

// The stored wire shape of an executed call's result (mcp_approval.py's `_mcp_result_to_json`):
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
    caller_principal: "haku-agent-api-token",
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
    username: "agentydragon",
    connected_at: "2026-07-01T09:00:00Z",
    token_expires_at: "2026-08-01T09:00:00Z",
    scope: "read write",
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

// The grocy-sf reference the preview widgets resolve id→name against, and (for products)
// read current field values from to render `products_edit` old→new diffs. Served by mock_api
// so the products_edit / shopping-list gallery entries render resolved, with an old side.
export const SAMPLE_GROCY_REFERENCE: GrocyReferenceResponse = {
  products: [
    {
      id: 1,
      name: "Rolled oats",
      location_id: 10,
      qu_id_stock: 20,
      qu_id_purchase: 20,
      qu_id_consume: 20,
      min_stock_amount: 250,
      default_best_before_days: 180,
      due_type: 1,
      parent_product_id: null,
      product_group_id: 30,
      description: "Thin rolled oats.",
      calories: null,
    },
    {
      id: 2,
      name: "Almond butter",
      location_id: 10,
      qu_id_stock: 21,
      qu_id_purchase: 22,
      qu_id_consume: 22,
      min_stock_amount: 0,
      default_best_before_days: 180,
      due_type: 1,
      parent_product_id: null,
      product_group_id: null,
      description: null,
      calories: null,
    },
  ],
  locations: [
    { id: 10, name: "Pantry" },
    { id: 11, name: "Fridge" },
    { id: 12, name: "Freezer" },
  ],
  quantity_units: [
    { id: 20, name: "gram" },
    { id: 21, name: "jar" },
    { id: 22, name: "case" },
    { id: 23, name: "carton" },
  ],
  product_groups: [
    { id: 30, name: "Snacks" },
    { id: 31, name: "Grains" },
    { id: 32, name: "Dairy" },
  ],
  shopping_lists: [
    { id: 40, name: "Weekly" },
    { id: 41, name: "Costco run" },
  ],
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
  // The stored result envelope for a finished call; absent = the call renders as pending.
  result?: StoredToolResult;
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
    // One row per input item; the last one fails so the gallery shows the red failed path.
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
      {
        kind: "ok",
        product_name: "Almond butter",
        transaction_id: "a13d77b0",
        amount_delta: 1,
        new_amount: 2,
        qu_name: "Jar",
        stock_qu_name: null,
        location_name: "Pantry",
        entry_id: 190,
        best_before_date: "2027-01-08",
      },
      {
        kind: "ok",
        product_name: "Frozen berries",
        transaction_id: "c58e01f4",
        amount_delta: 3,
        new_amount: 3,
        qu_name: "Bag",
        stock_qu_name: null,
        location_name: "Freezer",
        entry_id: 191,
        best_before_date: "2027-07-09",
      },
      {
        kind: "ok",
        product_name: "Oat milk",
        transaction_id: "9b24aa61",
        amount_delta: 6,
        new_amount: 8,
        qu_name: "Carton",
        stock_qu_name: null,
        location_name: "Fridge",
        entry_id: 192,
        best_before_date: "2026-08-02",
      },
      { kind: "error", error: "No product 'Dark chocolate' found — create it with products_create first." },
    ]),
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
    result: callToolResult([
      { kind: "ok", created_object_id: 201 },
      { kind: "ok", created_object_id: 202 },
    ]),
  },
  {
    title: "Update pantry product settings",
    serverId: "grocy-sf",
    toolName: "products_edit",
    args: {
      items: [
        {
          product: "Rolled oats",
          location: "Fridge",
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
    title: "Check the weekly shopping list",
    serverId: "grocy-sf",
    toolName: "shopping_list_get",
    args: { shopping_list: "Weekly" },
  },
  {
    title: "Add items to the shopping list",
    serverId: "grocy-sf",
    toolName: "shopping_list_items_add",
    args: {
      items: [
        { shopping_list: "Weekly", product: "Rolled oats", amount: 2 },
        { shopping_list: "Weekly", amount: 1, note: "check if we need paper towels" },
        { shopping_list: "Costco run", product: "Almond butter", amount: 1, note: "the crunchy kind" },
      ],
    },
    // One result row per input item; the note-only item has a null product/unit.
    result: callToolResult([
      { kind: "ok", item_id: 55, product_name: "Rolled oats", amount: 2, qu_name: "Pack" },
      { kind: "ok", item_id: 56, product_name: null, amount: 1, qu_name: null },
      { kind: "ok", item_id: 57, product_name: "Almond butter", amount: 1, qu_name: "Jar" },
    ]),
  },
  {
    title: "Bump a shopping-list item to family size",
    serverId: "grocy-sf",
    toolName: "shopping_list_item_edit",
    args: { item_id: 42, amount: 3, note: "family size", done: true },
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
    result: callToolResult({
      event_id: "0k5rq2n8vd1m3jf7",
      html_link: "https://www.google.com/calendar/event?eid=MGs1cnEybjh2ZDFtM2pmNyBmYW1pbHlAZ3JvdXA",
    }),
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
      body: "Hi team,\n\nThanks for the notes. A few thoughts on the roadmap:\n- Ship the console Settings panel\n- Then the previews gallery\n- Circle back on datetime formatting\n\nBest,\nRai",
      thread_id: "thread-42",
    },
    result: callToolResult({
      id: "r-2603837261749773001",
      message: { id: "18c9f7a2b3d4e5f6", threadId: "thread-42", labelIds: ["DRAFT"] },
    }),
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
] satisfies (RegisteredToolPreviewFixture & { title: string; result?: StoredToolResult })[];

const SHOPPING_REMOVE_PREVIEW_SAMPLE = {
  title: "Remove purchased shopping-list items",
  serverId: "grocy-sf",
  toolName: "shopping_list_items_remove",
  args: { item_ids: [3, 7, 12, 15, 21, 34, 42, 55] },
  result: callToolResult([
    { kind: "ok", item_id: 3, product_name: "Milk", amount: 1, qu_name: "Carton" },
    { kind: "ok", item_id: 7, product_name: "Spinach", amount: 200, qu_name: "Gram" },
  ]),
} satisfies RegisteredToolPreviewFixture & { title: string; result: StoredToolResult };

export const PREVIEW_SAMPLES: PreviewSample[] = [...CUSTOM_PREVIEW_SAMPLES, SHOPPING_REMOVE_PREVIEW_SAMPLE];
