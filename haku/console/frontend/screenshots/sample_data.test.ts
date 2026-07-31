import { describe, expect, it } from "vitest";

import { recentToolCallTtlMs } from "../approval_state";
import { toolCallPreview, toolPreview } from "../tool_rendering/index";
import { SAMPLE_PENDING, SAMPLE_TOOL_CALLS, sampleRecentToolCalls } from "./sample_data";

const CUSTOM_HISTORY_IDS = new Set(["tc_1", "tc_2", "tc_3"]);
const CUSTOM_PENDING_IDS = new Set(["tc_p2"]);

// A fixture is either an args-only widget (toolPreview) or a combined pending/finished one
// (toolCallPreview) — mirrors ToolCallCard's own dispatch order.
function expectCustomPreview(serverId: string, toolName: string, args: Record<string, unknown>): void {
  for (const variant of ["compact", "detailed"] as const) {
    const node =
      toolCallPreview(serverId, toolName, args, null, variant) ?? toolPreview(serverId, toolName, args, variant);
    expect(node, `${serverId}.${toolName} (${variant})`).not.toBeNull();
  }
}

describe("screenshot tool-call fixtures", () => {
  it("dispatches every intended history and approval fixture to a custom preview", () => {
    const customHistory = SAMPLE_TOOL_CALLS.filter(({ tool_call_id }) => CUSTOM_HISTORY_IDS.has(tool_call_id));
    const customPending = SAMPLE_PENDING.filter(({ tool_call_id }) => CUSTOM_PENDING_IDS.has(tool_call_id));
    expect(customHistory).toHaveLength(CUSTOM_HISTORY_IDS.size);
    expect(customPending).toHaveLength(CUSTOM_PENDING_IDS.size);

    for (const record of customHistory) {
      expectCustomPreview(record.server_id, record.tool_name, record.arguments ?? {});
    }
    for (const approval of customPending) {
      expectCustomPreview(approval.server_id, approval.tool_name, approval.arguments ?? {});
    }
  });

  it("constructs recent calls relative to the render clock", () => {
    const nowMs = 20_000;
    const [recent] = sampleRecentToolCalls(nowMs);
    expect(recent.hideAtMs).toBe(nowMs + (recentToolCallTtlMs(recent.record.status) ?? 0));
  });
});
