import { afterEach, describe, expect, it, vi } from "vitest";

import type { SandboxView, ThreadView } from "../client";
import { threadStatusFromSnapshot } from "./thread_status";

afterEach(() => vi.restoreAllMocks());

const SANDBOX_UID = "00000000-0000-4000-8000-000000000001";

const READY_SANDBOX: SandboxView = {
  name: "test-sandbox",
  uid: SANDBOX_UID,
  namespace: "agentplane-test",
  created_at: "2026-01-01T00:00:00Z",
  binding: null,
  preset: null,
  operating_mode: "Running",
  wake_on_send: false,
  idle_suspend_after_seconds: null,
  suspended_by: null,
  service_account: { namespace: "agentplane-test", name: "test-sandbox" },
  status: { conditions: [{ type: "Ready", status: "True" }] },
  phase: "Running",
  pod: {
    name: "test-sandbox",
    namespace: "agentplane-test",
    uid: `${SANDBOX_UID}-pod`,
    deleting: false,
    node_name: "test-node",
    owner_references: [
      {
        api_version: "agentplane.allegedly.works/v1alpha1",
        kind: "Sandbox",
        name: "test-sandbox",
        uid: SANDBOX_UID,
        controller: true,
      },
    ],
    status: { phase: "Running", podIP: "10.0.0.1", conditions: [{ type: "Ready", status: "True" }] },
  },
};

const SUSPENDED_SANDBOX: SandboxView = {
  ...READY_SANDBOX,
  operating_mode: "Suspended",
  status: {},
  phase: "Suspended",
  pod: null,
};

function thread(overrides: Partial<ThreadView> = {}): ThreadView {
  return {
    id: "10000000-0000-4000-8000-000000000001",
    sandbox: READY_SANDBOX.name,
    harness: "HARNESS_CLAUDE",
    model: "test-model",
    cwd: "/work",
    created_at: "2026-01-01T00:00:00Z",
    name: null,
    archived: false,
    last_cursor: 3,
    last_event_at: null,
    last_model_activity_at: null,
    idle_since: null,
    harness_state: "HARNESS_STATE_RUNNING",
    setup_state: null,
    launch_outcome: null,
    active_turn_id: null,
    reasoning_effort: null,
    feed_status: "active",
    last_turn_status: null,
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
    // A Thread whose setup script runs has no harness yet, whether or not the fold has ended its feed.
    [{ ...failed, harness_state: "HARNESS_STATE_STOPPED", setup_state: "SETUP_STATE_RUNNING" }, "inactive"],
    [
      { ...failed, harness_state: "HARNESS_STATE_STOPPED", setup_state: "SETUP_STATE_RUNNING", feed_status: "ended" },
      "inactive",
    ],
    [
      {
        ...failed,
        feed_status: "active",
        harness_state: "HARNESS_STATE_STOPPED",
        setup_state: "SETUP_STATE_SUCCEEDED",
        launch_outcome: "LAUNCH_OUTCOME_PENDING",
      },
      "stopped",
    ],
    [
      {
        ...failed,
        harness_state: "HARNESS_STATE_STOPPED",
        setup_state: "SETUP_STATE_SUCCEEDED",
        launch_outcome: "LAUNCH_OUTCOME_STARTED",
      },
      "stopped",
    ],
  ] as const)("%o is %s", (overrides, kind) => {
    expect(threadStatusFromSnapshot(thread(overrides), READY_SANDBOX, true).kind).toBe(kind);
  });

  it("is inactive when the sandbox is unavailable, the snapshot is stale, or the thread is unknown", () => {
    expect(threadStatusFromSnapshot(thread(failed), undefined, true).kind).toBe("inactive");
    expect(threadStatusFromSnapshot(thread(failed), READY_SANDBOX, false).kind).toBe("inactive");
    expect(threadStatusFromSnapshot(undefined, READY_SANDBOX, true).kind).toBe("inactive");
  });

  it("shows a suspended Sandbox as a distinct thread status", () => {
    expect(threadStatusFromSnapshot(thread(failed), SUSPENDED_SANDBOX, true)).toEqual({
      kind: "suspended",
      label: "Sandbox suspended",
      tabLabel: "Suspended",
    });
  });
});
