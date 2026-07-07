import { describe, expect, it } from "vitest";

import {
  approvalDisplayFields,
  approvalQueueItems,
  geolocationApprovalQueueId,
  makeRecentToolCall,
  recentToolCallCountdown,
  toolApprovalQueueId,
  type GeolocationApproval,
} from "./approval_state.ts";
import type { PendingApproval, ToolCallRecord } from "./client.ts";

function pendingApproval(overrides: Partial<PendingApproval> = {}): PendingApproval {
  return {
    tool_call_id: "tc_1",
    server_id: "grocy-sf",
    title: "Remove bought items",
    tool_name: "shopping_list_items_remove",
    caller_principal: "haku-agent-api-token",
    rationale: "already in stock",
    arguments: { ids: [1, 2, 3] },
    created_at: "2026-07-07T10:00:00Z",
    ...overrides,
  };
}

function geolocationApproval(overrides: Partial<GeolocationApproval> = {}): GeolocationApproval {
  return {
    id: "geo_1",
    mode: "geolocation",
    createdAt: "2026-07-07T10:01:00Z",
    ...overrides,
  };
}

function toolCallRecord(overrides: Partial<ToolCallRecord> = {}): ToolCallRecord {
  return {
    tool_call_id: "tc_1",
    server_id: "grocy-sf",
    tool_name: "shopping_list_items_remove",
    caller_principal: "operator",
    status: "ok",
    created_at: "2026-07-07T10:00:00Z",
    updated_at: "2026-07-07T10:00:10Z",
    arguments: { ids: [1, 2, 3] },
    rationale: "already in stock",
    title: "Remove bought items",
    result: { removed: 3 },
    error: null,
    ...overrides,
  };
}

describe("approval queue state", () => {
  it("orders mixed tool and geolocation approvals newest first", () => {
    const items = approvalQueueItems(
      [pendingApproval({ tool_call_id: "tc_old", created_at: "2026-07-07T10:00:00Z" })],
      [geolocationApproval({ id: "geo_new", createdAt: "2026-07-07T10:02:00Z" })]
    );

    expect(items.map((item) => item.id)).toEqual([
      geolocationApprovalQueueId("geo_new"),
      toolApprovalQueueId("tc_old"),
    ]);
  });

  it("extracts structured display fields without collapsing everything into one JSON blob", () => {
    const fields = approvalDisplayFields(pendingApproval());

    expect(fields.serverId).toBe("grocy-sf");
    expect(fields.toolName).toBe("shopping_list_items_remove");
    expect(fields.argumentSummary).toBe("ids");
    expect(fields.argumentsJson).toContain('"ids"');
    expect(fields.toolCallId).toBe("tc_1");
  });

  it("keeps terminal results only as short-lived recent feedback", () => {
    const recent = makeRecentToolCall(toolCallRecord(), 1_000);

    expect(recent?.hideAtMs).toBe(16_000);
    expect(recent ? recentToolCallCountdown(recent, 6_500) : null).toMatchObject({
      label: "Auto-hides in 10s",
      remainingSeconds: 10,
    });
    expect(recent ? recentToolCallCountdown(recent, 6_500).progressPercent : null).toBeCloseTo(63.33, 1);
    expect(makeRecentToolCall(toolCallRecord({ status: "pending_approval" }), 1_000)).toBeNull();
  });

  it("uses a longer countdown for errored tool calls", () => {
    const recent = makeRecentToolCall(toolCallRecord({ status: "error" }), 1_000);

    expect(recent?.hideAtMs).toBe(61_000);
    expect(recent ? recentToolCallCountdown(recent, 70_000) : null).toMatchObject({
      label: "Auto-hides now",
      progressPercent: 0,
      remainingSeconds: 0,
    });
  });
});
