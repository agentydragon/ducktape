import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { describe, expect, it } from "vitest";

import { SessionSchema } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { HarnessAttachment_State } from "../../../harness_host/harness_host_pb";
import { FeedStatus } from "../../../history_service/api/history/history_service_pb";
import { TurnStatus } from "../../../proto/event_pb";
import { HarnessState, SetupState } from "../../../proto/session_pb";
import { observedHostedSession, threadView, type ObservedHostedSessionInit } from "./testing/state";
import { archiveBlockReason, observedSession, threadStatusFromSnapshot } from "./thread_status";

const ID = "10000000-0000-4000-8000-000000000001";
type Activity = NonNullable<NonNullable<MessageInitShape<typeof SessionSchema>["status"]>["activity"]>;

const thread = (activity: Activity) =>
  threadView({ metadata: { threadId: ID }, session: { sessionId: ID, status: { activity } } });

const hosted = (init: ObservedHostedSessionInit = {}) =>
  threadView({ metadata: { threadId: ID }, session: observedHostedSession(ID, init) });

describe("Dispatcher activity owns current Thread status", () => {
  it.each([
    [hosted({ activeTurnId: "turn" }), "running"],
    [hosted({ pendingCommands: 2 }), "pending"],
    [hosted({ setup: SetupState.RUNNING }), "setting_up"],
    [hosted({ setup: SetupState.FAILED }), "failed"],
    [hosted({ setup: SetupState.INTERRUPTED }), "failed"],
    [hosted({ harnessState: HarnessAttachment_State.STARTING }), "pending"],
    [hosted({ harnessState: HarnessAttachment_State.DETACHED }), "stopped"],
    [hosted({ harnessState: HarnessAttachment_State.DETACHED, failureCursor: 15n }), "failed"],
    [hosted(), "idle"],
    [thread({ kind: { case: "unknown", value: { reason: 1 } } }), "unknown"],
    [thread({ kind: { case: "unknown", value: { reason: 2 } } }), "unknown"],
    [thread({ kind: { case: "environmentDown", value: { cause: 1 } } }), "suspended"],
    [thread({ kind: { case: "environmentDown", value: { cause: 2 } } }), "environment_down"],
    [thread({ kind: { case: "environmentDown", value: {} } }), "unknown"],
    [hosted({ setup: SetupState.UNSPECIFIED }), "unknown"],
    [hosted({ harnessState: HarnessAttachment_State.UNSPECIFIED }), "unknown"],
    [thread({}), "unknown"],
  ])("maps the authoritative observation to %s", (value, kind) => {
    // History can lag independently and claim the opposite of the host's observation.
    value.summary.attached!.activeTurnId = "old-turn";
    value.summary.attached!.harnessState = HarnessState.STOPPED;
    value.summary.attached!.setupState = SetupState.RUNNING;
    value.summary.feedStatus = FeedStatus.FAILED;
    expect(threadStatusFromSnapshot(value, true).kind).toBe(kind);
  });

  it("forgets a live mark immediately when the Dispatcher watch drops", () => {
    const value = hosted({ activeTurnId: "turn" });
    expect(threadStatusFromSnapshot(value, true).kind).toBe("running");
    expect(threadStatusFromSnapshot(value, false)).toMatchObject({ kind: "unknown", tabLabel: "Unknown" });
    expect(observedSession(value, false)).toBeUndefined();
    expect(archiveBlockReason(hosted(), false)).toContain("activity to be known");
  });

  it("does not accept another Session or host's observation", () => {
    const value = hosted();
    value.session!.status!.activity!.kind = {
      case: "observed",
      value: {
        ...observedSession(value, true)!,
        sessionId: "other-session",
      },
    };
    expect(threadStatusFromSnapshot(value, true).kind).toBe("unknown");
    const mismatch = hosted();
    mismatch.session!.placement = create(SessionSchema, {
      placement: { kind: { case: "centralSamplerV1", value: {} } },
    }).placement;
    expect(threadStatusFromSnapshot(mismatch, true).kind).toBe("unknown");
  });

  it("keeps the App's archived state independent of host availability", () => {
    const value = hosted();
    value.metadata.archived = true;
    expect(threadStatusFromSnapshot(value, false).kind).toBe("archived");
  });

  it.each([TurnStatus.FAILED, TurnStatus.PROCESS_LOST])(
    "shows the retained turn outcome only for a currently idle Session",
    (lastTurnStatus) => {
      const idle = hosted();
      idle.summary.lastTurnStatus = lastTurnStatus;
      expect(threadStatusFromSnapshot(idle, true).kind).toBe("turn_error");
      const dormant = hosted({ harnessState: HarnessAttachment_State.DETACHED });
      dormant.summary.lastTurnStatus = lastTurnStatus;
      expect(threadStatusFromSnapshot(dormant, true).kind).toBe("stopped");
    }
  );

  it("shows central blocked activity independently of its executor Sandbox", () => {
    const value = hosted();
    value.session!.placement = create(SessionSchema, {
      placement: { kind: { case: "centralSamplerV1", value: {} } },
    }).placement;
    value.session!.status!.activity = create(SessionSchema, {
      status: {
        activity: {
          kind: {
            case: "observed",
            value: {
              sessionId: ID,
              host: {
                case: "centralSamplerV1",
                value: {
                  setup: SetupState.NOT_REQUIRED,
                  blocked: { detail: "Effects unconfirmed", cause: { case: "unconfirmedEffects", value: {} } },
                },
              },
            },
          },
        },
      },
    }).status!.activity;
    expect(threadStatusFromSnapshot(value, true)).toMatchObject({
      kind: "blocked",
      label: "Session blocked · Effects unconfirmed",
    });
  });
});

it.each([
  [hosted({ activeTurnId: "turn" }), "active turn"],
  [hosted({ pendingCommands: 1 }), "pending commands"],
  [hosted({ setup: SetupState.RUNNING }), "setup"],
  [hosted({ harnessState: HarnessAttachment_State.STARTING }), "harness to start"],
  [thread({ kind: { case: "unknown", value: {} } }), "activity to be known"],
])("blocks archiving for authoritative unfinished work", (value, reason) => {
  expect(archiveBlockReason(value, true)).toContain(reason);
});

it.each([
  hosted(),
  hosted({ harnessState: HarnessAttachment_State.DETACHED }),
  thread({ kind: { case: "environmentDown", value: { cause: 1 } } }),
])("allows archiving confirmed idle, dormant or down Sessions", (value) => {
  value.summary.attached!.activeTurnId = "stale-history-turn";
  expect(archiveBlockReason(value, true)).toBeNull();
});
