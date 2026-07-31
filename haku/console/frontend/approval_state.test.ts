import { describe, expect, it } from "vitest";

import {
  approvalDisplayFields,
  approvalQueueItems,
  geolocationApprovalQueueId,
  makeRecentToolCall,
  recentToolCallCountdown,
  recentToolCallTtlMs,
  screenshotApprovalQueueId,
  showsAutoApprovalEvaluation,
  statusColor,
  terminalStatusLabel,
  toolApprovalQueueId,
  type GeolocationApproval,
  type ScreenshotApproval,
} from "./approval_state";
import type { ToolCallRecord } from "./client";

function pendingApproval(overrides: Partial<ToolCallRecord> = {}): ToolCallRecord {
  return {
    tool_call_id: "tc_1",
    server_id: "grocy-sf",
    title: "Remove bought items",
    tool_name: "shopping_list_items_remove",
    caller: {
      kind: "agent",
      agent_id: "11111111-1111-4111-8111-111111111111",
      display_name: "Haku agent",
    },
    rationale: "already in stock",
    arguments: { ids: [1, 2, 3] },
    status: "pending_approval",
    created_at: "2026-07-07T10:00:00Z",
    updated_at: "2026-07-07T10:00:00Z",
    result: null,
    error: null,
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

function screenshotApproval(overrides: Partial<ScreenshotApproval> = {}): ScreenshotApproval {
  return {
    id: "shot_1",
    createdAt: "2026-07-12T10:01:00Z",
    ...overrides,
  };
}

function toolCallRecord(overrides: Partial<ToolCallRecord> = {}): ToolCallRecord {
  return {
    tool_call_id: "tc_1",
    server_id: "grocy-sf",
    tool_name: "shopping_list_items_remove",
    caller: { kind: "operator" },
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
      [geolocationApproval({ id: "geo_new", createdAt: "2026-07-07T10:02:00Z" })],
      []
    );

    expect(items.map((item) => item.id)).toEqual([
      geolocationApprovalQueueId("geo_new"),
      toolApprovalQueueId("tc_old"),
    ]);
  });

  it("mixes in screenshot approvals alongside tool and geolocation ones", () => {
    const items = approvalQueueItems([], [], [screenshotApproval({ id: "shot_1", createdAt: "2026-07-12T10:01:00Z" })]);

    expect(items.map((item) => item.id)).toEqual([screenshotApprovalQueueId("shot_1")]);
    expect(items[0].kind).toBe("screenshot");
  });

  it("extracts structured display fields without collapsing everything into one JSON blob", () => {
    const fields = approvalDisplayFields(pendingApproval());

    expect(fields.serverId).toBe("grocy-sf");
    expect(fields.toolName).toBe("shopping_list_items_remove");
    expect(fields.argumentsJson).toContain('"ids"');
    expect(fields.toolCallId).toBe("tc_1");
    expect(fields.callerDisplayName).toBe("Haku agent");
    expect(fields.callerAgentId).toBe("11111111-1111-4111-8111-111111111111");
  });

  it("surfaces auto-approval policy provenance for terminal calls", () => {
    const fields = approvalDisplayFields(
      toolCallRecord({
        approval_policy_id: "v1",
        auto_approval_evaluation: "approved: Gmail search/read operation",
      })
    );
    expect(fields.approvalPolicyId).toBe("v1");
    expect(fields.autoApprovalEvaluation).toBe("approved: Gmail search/read operation");
  });

  it("hides a declined policy's evaluation in compact but keeps an auto-approval's", () => {
    const declined = { autoApprovalEvaluation: "manual: not a read-only tool", approvalPolicyId: null };
    const approved = { autoApprovalEvaluation: "approved: allowlisted read", approvalPolicyId: "unconditional_v1" };

    // A declined evaluation only explains an absence — the reason the operator is reading the card
    // at all — so it is provenance the compact view omits.
    expect(showsAutoApprovalEvaluation(declined, false)).toBe(false);
    expect(showsAutoApprovalEvaluation(declined, true)).toBe(true);
    // What let a call through unattended is worth a compact line.
    expect(showsAutoApprovalEvaluation(approved, false)).toBe(true);
    expect(showsAutoApprovalEvaluation(approved, true)).toBe(true);
    expect(showsAutoApprovalEvaluation({ autoApprovalEvaluation: null, approvalPolicyId: null }, true)).toBe(false);
  });

  it("labels an agent withdrawal distinctly from an operator denial", () => {
    expect(terminalStatusLabel("withdrawn")).toBe("Withdrawn");
    expect(terminalStatusLabel("denied")).toBe("Denied");
    expect(statusColor("withdrawn")).toBe("gray");
    expect(
      approvalDisplayFields(pendingApproval({ status: "withdrawn", withdrawal_reason: "superseded" }))
    ).toMatchObject({ withdrawalReason: "superseded", denialReason: null });
  });

  it("never shows a withdrawal as recent operator feedback", () => {
    // The Recent list is fed only by the operator's own decisions, and a withdrawal is not one.
    expect(recentToolCallTtlMs("withdrawn")).toBeNull();
    expect(makeRecentToolCall(pendingApproval({ status: "withdrawn" }), 1_000)).toBeNull();
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
