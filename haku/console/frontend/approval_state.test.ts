import { describe, expect, it } from "vitest";

import { capabilityRequestItems, geolocationApprovalQueueId, screenshotApprovalQueueId } from "./approval_state";
import { approvalDisplayFields, showsAutoApprovalEvaluation } from "./tool_call_state";
import type { ToolCallRecord } from "./tool_call_record";

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

describe("capability request state", () => {
  it("orders mixed geolocation and screenshot requests newest first", () => {
    const items = capabilityRequestItems(
      [{ id: "geo_old", mode: "geolocation", createdAt: "2026-07-07T10:00:00Z" }],
      [{ id: "shot_new", createdAt: "2026-07-12T10:01:00Z" }]
    );

    expect(items.map((item) => item.id)).toEqual([
      screenshotApprovalQueueId("shot_new"),
      geolocationApprovalQueueId("geo_old"),
    ]);
  });
});

describe("tool call display", () => {
  it("hides a declined policy's evaluation in compact but keeps an auto-approval's", () => {
    const declined = { autoApprovalEvaluation: "manual: not a read-only tool", approvalPolicyId: null };
    const approved = { autoApprovalEvaluation: "approved: allowlisted read", approvalPolicyId: "unconditional_v1" };

    // A declined evaluation is provenance: it only explains the absence the operator is looking at.
    expect(showsAutoApprovalEvaluation(declined, false)).toBe(false);
    expect(showsAutoApprovalEvaluation(declined, true)).toBe(true);
    // What let a call through unattended is worth a compact line.
    expect(showsAutoApprovalEvaluation(approved, false)).toBe(true);
    expect(showsAutoApprovalEvaluation(approved, true)).toBe(true);
    expect(showsAutoApprovalEvaluation({ autoApprovalEvaluation: null, approvalPolicyId: null }, true)).toBe(false);
  });

  it("attributes a call to the agent that made it, or to the operator", () => {
    expect(approvalDisplayFields(pendingApproval()).callerDisplayName).toBe("Haku agent");
    expect(approvalDisplayFields(pendingApproval({ caller: { kind: "operator" } })).callerDisplayName).toBe("Operator");
  });
});
