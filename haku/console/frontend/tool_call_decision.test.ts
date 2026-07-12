import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import type { PendingApproval, ToolCallRecord } from "./client.ts";
import { executeToolCallDecision, toolCallDecisionFeedback, useToolCallDecision } from "./tool_call_decision.ts";

function pendingApproval(overrides: Partial<PendingApproval> = {}): PendingApproval {
  return {
    tool_call_id: "tc_1",
    server_id: "grocy-sf",
    title: "Remove bought items",
    tool_name: "shopping_list_items_remove",
    caller_principal: "operator",
    rationale: "already in stock",
    arguments: { ids: [1, 2, 3] },
    created_at: "2026-07-07T10:00:00Z",
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

function dependencies() {
  return {
    approve: vi.fn(),
    deny: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
  };
}

describe("tool-call decision controller", () => {
  it("approves and reports the canonical custom title", async () => {
    const deps = dependencies();
    const record = toolCallRecord();
    deps.approve.mockResolvedValue(record);

    await expect(executeToolCallDecision(pendingApproval(), "approve", undefined, deps)).resolves.toBe(record);

    expect(deps.approve).toHaveBeenCalledWith("tc_1");
    expect(deps.deny).not.toHaveBeenCalled();
    expect(deps.success).toHaveBeenCalledWith("Tool call finished", "Remove bought items: OK");
    expect(deps.error).not.toHaveBeenCalled();
  });

  it("denies without inventing a reason and uses the same canonical title", async () => {
    const deps = dependencies();
    const record = toolCallRecord({ status: "denied", result: null });
    deps.deny.mockResolvedValue(record);

    await expect(executeToolCallDecision(pendingApproval(), "deny", undefined, deps)).resolves.toBe(record);

    expect(deps.deny).toHaveBeenCalledWith("tc_1", undefined);
    expect(deps.approve).not.toHaveBeenCalled();
    expect(deps.success).toHaveBeenCalledWith("Tool call denied", "Remove bought items: Denied");
  });

  it("uses approvalDisplayFields' fallback title", () => {
    expect(toolCallDecisionFeedback("approve", toolCallRecord({ title: null }))).toEqual({
      title: "Tool call finished",
      message: "grocy-sf: shopping_list_items_remove: OK",
    });
  });

  it("surfaces decision failures through one error path", async () => {
    const deps = dependencies();
    const failure = new Error("operator authentication expired");
    deps.approve.mockRejectedValue(failure);

    await expect(executeToolCallDecision(pendingApproval(), "approve", undefined, deps)).resolves.toBeNull();

    expect(deps.success).not.toHaveBeenCalled();
    expect(deps.error).toHaveBeenCalledWith("Tool call decision failed", failure);
  });

  it("tracks the same running transition until either surface's request settles", async () => {
    const deps = dependencies();
    const record = toolCallRecord();
    let resolveApproval: (record: ToolCallRecord) => void = () => undefined;
    deps.approve.mockReturnValue(
      new Promise<ToolCallRecord>((resolve) => {
        resolveApproval = resolve;
      })
    );
    const onSuccess = vi.fn();
    const onSettled = vi.fn();
    const controller: { current: ReturnType<typeof useToolCallDecision> | null } = { current: null };
    const root = createRoot(document.createElement("div"));
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

    function Harness() {
      controller.current = useToolCallDecision({ dependencies: deps, onSuccess, onSettled });
      return null;
    }

    act(() => root.render(createElement(Harness)));
    let decision: Promise<void> | null = null;
    act(() => {
      decision = controller.current?.approve(pendingApproval()) ?? null;
    });
    expect(controller.current?.isDeciding("tc_1")).toBe(true);

    await act(async () => {
      resolveApproval(record);
      await decision;
    });
    expect(controller.current?.isDeciding("tc_1")).toBe(false);
    expect(onSuccess).toHaveBeenCalledWith(record);
    expect(onSettled).toHaveBeenCalledOnce();
    act(() => root.unmount());
  });
});
