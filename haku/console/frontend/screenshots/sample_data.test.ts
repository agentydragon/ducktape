import { describe, expect, it } from "vitest";

import { recentToolCallTtlMs } from "../approval_state.ts";
import { toolPreview } from "../tool_rendering/index.tsx";
import { unwrapToolResult } from "../tool_rendering/result_entry.tsx";
import { toolResultPreview } from "../tool_rendering/index.tsx";
import { PREVIEW_SAMPLES, SAMPLE_PENDING, SAMPLE_TOOL_CALLS, sampleRecentToolCalls } from "./sample_data.ts";

const CUSTOM_HISTORY_IDS = new Set(["tc_1", "tc_2", "tc_3"]);
const CUSTOM_PENDING_IDS = new Set(["tc_p2"]);

function expectCustomPreview(serverId: string, toolName: string, args: Record<string, unknown>): void {
  for (const variant of ["compact", "detailed"] as const) {
    expect(toolPreview(serverId, toolName, args, variant), `${serverId}.${toolName} (${variant})`).not.toBeNull();
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

  it("dispatches every gallery fixture", () => {
    for (const { serverId, toolName, args } of PREVIEW_SAMPLES) {
      expectCustomPreview(serverId, toolName, args);
    }
  });

  it("dispatches every gallery result payload to a custom result preview", () => {
    const withResults = PREVIEW_SAMPLES.filter(({ result }) => result != null);
    // Covers every server with a result widget plus both fallback paths.
    expect(withResults.length).toBeGreaterThanOrEqual(5);
    for (const { serverId, toolName, result } of withResults) {
      const key = `${serverId}.${toolName}`;
      const payload = unwrapToolResult(result);
      expect(payload, key).not.toBeNull();
      for (const variant of ["compact", "detailed"] as const) {
        const node = toolResultPreview(serverId, toolName, payload, variant);
        expect(node, `${key} (${variant})`).not.toBeNull();
      }
    }
  });

  it("constructs recent calls relative to the render clock", () => {
    const nowMs = 20_000;
    const [recent] = sampleRecentToolCalls(nowMs);
    expect(recent.hideAtMs).toBe(nowMs + (recentToolCallTtlMs(recent.record.status) ?? 0));
  });
});
