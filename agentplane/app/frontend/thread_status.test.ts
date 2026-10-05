import { afterEach, describe, expect, it, vi } from "vitest";

import type { SandboxView, ThreadView } from "./client";
import { threadStatusFromSnapshot } from "./thread_status";

afterEach(() => vi.restoreAllMocks());

const SANDBOX_UID = "00000000-0000-4000-8000-000000000001";

const READY_SANDBOX: SandboxView = {
  name: "test-sandbox",
  uid: SANDBOX_UID,
  namespace: "agentplane-test",
  created_at: "2026-01-01T00:00:00Z",
  operating_mode: "Running",
  service_account: { namespace: "agentplane-test", name: "test-sandbox" },
  status: null,
  kubernetes_grants: [],
  kubernetes_grants_ready: true,
  kubernetes_grant_error: null,
  launch_grants_pending: false,
  deleting: false,
  pod: {
    name: "test-sandbox",
    namespace: "agentplane-test",
    uid: `${SANDBOX_UID}-pod`,
    deleting: false,
    node_name: "test-node",
    owner_references: [
      {
        api_version: "agents.x-k8s.io/v1beta1",
        kind: "Sandbox",
        name: "test-sandbox",
        uid: SANDBOX_UID,
        controller: true,
      },
    ],
    status: { phase: "Running", podIP: "10.0.0.1", conditions: [{ type: "Ready", status: "True" }] },
  },
};

function thread(overrides: Partial<ThreadView> = {}): ThreadView {
  return {
    id: "10000000-0000-4000-8000-000000000001",
    sandbox: READY_SANDBOX.name,
    session_id: "test-session",
    harness: "HARNESS_CLAUDE",
    model: "test-model",
    cwd: "/work",
    created_at: "2026-01-01T00:00:00Z",
    name: null,
    archived: false,
    last_cursor: 3,
    harness_state: "HARNESS_STATE_RUNNING",
    feed_status: "active",
    ...overrides,
  };
}

describe("a thread whose harness is live and waiting", () => {
  it.each([
    ["TURN_STATUS_FAILED", "turn_error"],
    ["TURN_STATUS_PROCESS_LOST", "turn_error"],
    ["TURN_STATUS_UNSPECIFIED", "turn_error"],
    ["TURN_STATUS_COMPLETED", "idle"],
    // An interrupt is the operator's own doing.
    ["TURN_STATUS_INTERRUPTED", "idle"],
    [null, "idle"],
    [undefined, "idle"],
  ] as const)("with last turn status %s is %s", (lastTurnStatus, kind) => {
    expect(threadStatusFromSnapshot(thread({ last_turn_status: lastTurnStatus }), READY_SANDBOX, true).kind).toBe(kind);
  });

  // A newer backend can add a TurnStatus member this bundle has no name for; it must not break the sidebar.
  it("is idle, and logs the status, for a last turn status that is not a TurnStatus member", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const status = threadStatusFromSnapshot(
      thread({ last_turn_status: "TURN_STATUS_NOT_A_MEMBER" }),
      READY_SANDBOX,
      true
    );
    expect(status.kind).toBe("idle");
    expect(warn).toHaveBeenCalledWith(expect.any(String), "TURN_STATUS_NOT_A_MEMBER");
  });
});

describe("a failed last turn colors only an otherwise idle thread", () => {
  const failed = { last_turn_status: "TURN_STATUS_FAILED" } as const;

  it.each([
    [{ ...failed, active_turn_id: "turn-1" }, "running"],
    [{ ...failed, harness_state: "HARNESS_STATE_STOPPED" }, "stopped"],
    [{ ...failed, harness_state: "HARNESS_STATE_UNSPECIFIED" }, "stopped"],
    [{ ...failed, harness_state: "HARNESS_STATE_STOPPED", feed_status: "ended" }, "stopped"],
    [{ ...failed, feed_status: "failed" }, "failed"],
    [{ ...failed, feed_status: "ended" }, "inactive"],
    [{ ...failed, feed_status: null }, "inactive"],
    [{ ...failed, archived: true }, "archived"],
  ] as const)("%o is %s", (overrides, kind) => {
    expect(threadStatusFromSnapshot(thread(overrides), READY_SANDBOX, true).kind).toBe(kind);
  });

  it("is inactive when the sandbox is unavailable, the snapshot is stale, or the thread is unknown", () => {
    expect(threadStatusFromSnapshot(thread(failed), undefined, true).kind).toBe("inactive");
    expect(threadStatusFromSnapshot(thread(failed), READY_SANDBOX, false).kind).toBe("inactive");
    expect(threadStatusFromSnapshot(undefined, READY_SANDBOX, true).kind).toBe("inactive");
  });
});
