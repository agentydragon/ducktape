// Deterministic sample data for the screenshot scenes (harness.tsx) and the API stub
// (mock_api.ts). Kept separate so both share one source of truth.
import type { RecentToolCall } from "../approval_state.ts";
import type { McpOperatorAuthStatus, PendingApproval, ToolCallRecord } from "../client.ts";

function toolCall(overrides: Partial<ToolCallRecord> & Pick<ToolCallRecord, "tool_call_id">): ToolCallRecord {
  return {
    server_id: "grocy-sf",
    tool_name: "stock_add",
    caller_principal: "haku-agent-api-token",
    status: "ok",
    created_at: "2026-07-09T14:32:00Z",
    updated_at: "2026-07-09T14:32:04Z",
    arguments: { items: [{ product_id: 42, amount: 2 }] },
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
    server_id: "google",
    tool_name: "create_calendar_event",
    title: "Create calendar event: Dentist",
    status: "error",
    rationale: "Booked a dentist appointment from the confirmation email.",
    error: "Calendar API returned 403: insufficient scope for calendar.events.",
    result: null,
    caller_principal: "operator",
    arguments: { summary: "Dentist", start: "2026-07-12T09:00:00", end: "2026-07-12T10:00:00" },
  }),
  toolCall({
    tool_call_id: "tc_3",
    server_id: "kubectl",
    tool_name: "delete_pod",
    title: "Delete crashlooping pod",
    status: "denied",
    rationale: "The worker pod has been CrashLoopBackOff for 20 minutes; restart it.",
    denial_reason: "Not without a rollout plan — investigate the crash first.",
    result: null,
    caller_principal: "operator",
    arguments: { namespace: "haku-sandbox", pod: "worker-6f9c2" },
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
];

export const SAMPLE_RECENT: RecentToolCall[] = [
  {
    record: toolCall({ tool_call_id: "tc_r1", title: "Add Thrive box items to Grocy", status: "ok" }),
    hideAtMs: 12_000,
  },
];

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
