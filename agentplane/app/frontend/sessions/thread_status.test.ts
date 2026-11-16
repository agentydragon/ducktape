import { afterEach, describe, expect, it, vi } from "vitest";

import { FeedStatus } from "../../../history_service/api/history/history_service_pb";
import { TurnStatus } from "../../../proto/event_pb";
import { HarnessState, LaunchOutcome, SetupState } from "../../../proto/session_pb";
import { threadView, type ThreadViewInit } from "./testing/state";
import { readySandbox, suspendedSandbox } from "../testing/sandbox";
import { threadStatusFromSnapshot } from "./thread_status";

afterEach(() => vi.restoreAllMocks());

const SANDBOX_UID = "00000000-0000-4000-8000-000000000001";
const THREAD_ID = "10000000-0000-4000-8000-000000000001";

const READY_SANDBOX = readySandbox("test-sandbox", SANDBOX_UID);
const SUSPENDED_SANDBOX = suspendedSandbox("test-sandbox", SANDBOX_UID);

function thread(changes: ThreadViewInit = {}) {
  return threadView({
    metadata: { threadId: THREAD_ID, archived: false, ...changes.metadata },
    summary: {
      sandbox: { name: READY_SANDBOX.target!.name, uid: SANDBOX_UID },
      feedStatus: FeedStatus.ACTIVE,
      ...changes.summary,
    },
  });
}

describe("a thread whose harness is live and waiting", () => {
  it.each([
    ["failed", TurnStatus.FAILED, "turn_error"],
    ["process lost", TurnStatus.PROCESS_LOST, "turn_error"],
    ["unspecified", TurnStatus.UNSPECIFIED, "turn_error"],
    ["completed", TurnStatus.COMPLETED, "idle"],
    // An interrupt is the operator's own doing.
    ["interrupted", TurnStatus.INTERRUPTED, "idle"],
    ["absent", undefined, "idle"],
  ] as const)("with last turn status %s is %s", (_label, lastTurnStatus, kind) => {
    expect(threadStatusFromSnapshot(thread({ summary: { lastTurnStatus } }), READY_SANDBOX, true).kind).toBe(kind);
  });

  // A newer backend can add a TurnStatus member this bundle has no name for; it must not break the sidebar.
  it("is idle, and logs the status, for a last turn status that is not a TurnStatus member", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const status = threadStatusFromSnapshot(
      thread({ summary: { lastTurnStatus: 99 as TurnStatus } }),
      READY_SANDBOX,
      true
    );
    expect(status.kind).toBe("idle");
    expect(warn).toHaveBeenCalledWith(expect.any(String), 99);
  });
});

describe("a failed last turn colors only an otherwise idle thread", () => {
  const failed = { lastTurnStatus: TurnStatus.FAILED };

  const cases: Array<[ThreadViewInit, string]> = [
    [{ summary: { ...failed, attached: { activeTurnId: "turn-1" } } }, "running"],
    [{ summary: { ...failed, attached: { harnessState: HarnessState.STOPPED } } }, "stopped"],
    [{ summary: { ...failed, attached: { harnessState: HarnessState.UNSPECIFIED } } }, "stopped"],
    [
      { summary: { ...failed, attached: { harnessState: HarnessState.STOPPED }, feedStatus: FeedStatus.ENDED } },
      "stopped",
    ],
    [{ summary: { ...failed, feedStatus: FeedStatus.FAILED } }, "failed"],
    [{ summary: { ...failed, feedStatus: FeedStatus.ENDED } }, "inactive"],
    [{ summary: { ...failed, feedStatus: FeedStatus.UNSPECIFIED } }, "inactive"],
    [{ summary: { ...failed, feedStatus: FeedStatus.ACTIVE, attached: undefined } }, "inactive"],
    [{ summary: { ...failed, attached: undefined } }, "inactive"],
    [{ metadata: { archived: true }, summary: failed }, "archived"],
    // A Thread whose setup script runs has no harness yet, whether or not the fold has ended its feed.
    [
      { summary: { ...failed, attached: { harnessState: HarnessState.STOPPED, setupState: SetupState.RUNNING } } },
      "inactive",
    ],
    [
      {
        summary: {
          ...failed,
          attached: { harnessState: HarnessState.STOPPED, setupState: SetupState.RUNNING },
          feedStatus: FeedStatus.ENDED,
        },
      },
      "inactive",
    ],
    [
      {
        summary: {
          ...failed,
          feedStatus: FeedStatus.ACTIVE,
          attached: {
            harnessState: HarnessState.STOPPED,
            setupState: SetupState.SUCCEEDED,
            launchOutcome: LaunchOutcome.PENDING,
          },
        },
      },
      "stopped",
    ],
    [
      {
        summary: {
          ...failed,
          attached: {
            harnessState: HarnessState.STOPPED,
            setupState: SetupState.SUCCEEDED,
            launchOutcome: LaunchOutcome.STARTED,
          },
        },
      },
      "stopped",
    ],
  ];
  it.each(cases)("%o is %s", (changes, kind) => {
    expect(threadStatusFromSnapshot(thread(changes), READY_SANDBOX, true).kind).toBe(kind);
  });

  it("is inactive when the sandbox is unavailable, the snapshot is stale, or the thread is unknown", () => {
    expect(threadStatusFromSnapshot(thread({ summary: failed }), undefined, true).kind).toBe("inactive");
    expect(threadStatusFromSnapshot(thread({ summary: failed }), READY_SANDBOX, false).kind).toBe("inactive");
    expect(threadStatusFromSnapshot(undefined, READY_SANDBOX, true).kind).toBe("inactive");
  });

  it("shows a suspended Sandbox as a distinct thread status", () => {
    expect(threadStatusFromSnapshot(thread({ summary: failed }), SUSPENDED_SANDBOX, true)).toEqual({
      kind: "suspended",
      label: "Sandbox suspended",
      tabLabel: "Suspended",
    });
  });
});

it("keeps a central sampler turn visible when the execution Sandbox is unavailable", () => {
  const central = thread({
    summary: {
      attached: {
        spec: {
          placement: {
            kind: {
              case: "centralSamplerV1",
              value: { executorEnvironment: { name: READY_SANDBOX.target!.name, uid: SANDBOX_UID } },
            },
          },
        },
        activeTurnId: "turn-central",
      },
    },
  });
  expect(threadStatusFromSnapshot(central, SUSPENDED_SANDBOX, true).kind).toBe("running");
  expect(threadStatusFromSnapshot(central, undefined, true).kind).toBe("running");
  expect(threadStatusFromSnapshot(central, undefined, false).kind).toBe("inactive");
});
