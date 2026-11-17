import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import { act } from "react";

import {
  ThreadEntitySchema,
  ThreadFeedStatus,
  ThreadPayloadField,
  ThreadPayloadReferenceSchema,
  type ThreadEntity,
  type ThreadPayloadReference,
  FeedStatus,
  type SandboxRef,
  SandboxRefSchema,
  type SessionSummary,
  SessionSummarySchema,
} from "../../../../history_service/api/history/history_service_pb";
import { ThreadMetadataSchema, type ThreadMetadata } from "../../../app_service_pb";
import {
  AttachedSchema,
  type Attached,
  Harness,
  HarnessState,
  LaunchOutcome,
  SessionPlacementSchema,
  SessionSpecSchema,
  SetupState,
} from "../../../../proto/session_pb";
import {
  SessionActivity_EnvironmentDown_Cause,
  SessionActivitySchema,
  SessionSchema,
  SessionStatus_Materialization,
  SessionStatus_RequiredEnvironment,
  SessionStatusSchema,
} from "../../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
// The Dispatcher package also exports Session, so name this host-side Session schema explicitly.
import {
  HarnessAttachment_State,
  SessionSchema as HarnessSessionSchema,
} from "../../../../harness_host/harness_host_pb";

import type { ThreadView } from "../client";
import type { ThreadState, ThreadSync } from "../thread_sync";

type PartialMessage<T extends { $typeName: string }> = Partial<Omit<T, "$typeName" | "$unknown">>;

export type ThreadViewInit = {
  metadata?: PartialMessage<ThreadMetadata>;
  summary?: PartialMessage<Omit<SessionSummary, "attached" | "sandbox">> & {
    attached?:
      | (PartialMessage<Omit<Attached, "spec">> & { spec?: MessageInitShape<typeof SessionSpecSchema> | undefined })
      | undefined;
    sandbox?: PartialMessage<SandboxRef> | undefined;
  };
  session?: MessageInitShape<typeof SessionSchema> | undefined;
};

export type ObservedHostedSessionInit = {
  activeTurnId?: string;
  pendingCommands?: number;
  setup?: SetupState;
  harnessState?: HarnessAttachment_State;
  failureCursor?: bigint;
};

/** A minimal Dispatcher activity override for an observed Environment Hosted Session. */
export function observedHostedSession(
  threadId: string,
  {
    activeTurnId = "",
    pendingCommands = 0,
    setup = SetupState.NOT_REQUIRED,
    harnessState = HarnessAttachment_State.ATTACHED,
    failureCursor,
  }: ObservedHostedSessionInit = {}
): MessageInitShape<typeof SessionSchema> {
  return {
    sessionId: threadId,
    status: {
      activity: {
        kind: {
          case: "observed",
          value: {
            sessionId: threadId,
            activeTurnId,
            pendingCommands,
            host: {
              case: "environmentHosted",
              value: {
                setup,
                harness: {
                  state: harnessState,
                  failureCursor,
                },
              },
            },
          },
        },
      },
    },
  };
}

/** Dispatcher activity for an Environment Hosted Session whose exact Environment is down. */
export function environmentDownSession(
  threadId: string,
  cause: SessionActivity_EnvironmentDown_Cause = SessionActivity_EnvironmentDown_Cause.SUSPENDED
): MessageInitShape<typeof SessionSchema> {
  return {
    sessionId: threadId,
    status: {
      activity: {
        kind: {
          case: "environmentDown",
          value: { cause },
        },
      },
    },
  };
}

export function threadView({ metadata = {}, summary = {}, ...init }: ThreadViewInit = {}): ThreadView {
  const threadId = metadata.threadId ?? summary.sessionId ?? "10000000-0000-4000-8000-000000000001";
  const sandbox = { name: "composer-test", uid: "20000000-0000-4000-8000-000000000001" };
  const sandboxInit = Object.hasOwn(summary, "sandbox") ? summary.sandbox : sandbox;
  const spec = {
    placement: create(SessionPlacementSchema, {
      kind: {
        case: "environmentHosted" as const,
        value: {
          environment: { name: sandboxInit?.name ?? sandbox.name, uid: sandboxInit?.uid ?? sandbox.uid },
          harness: Harness.CLAUDE,
        },
      },
    }),
    model: "test-model",
    cwd: "/test-workspace",
    reasoningEffort: "low",
  };
  const attachedInit = Object.hasOwn(summary, "attached") ? summary.attached : {};
  const specInit = attachedInit !== undefined && Object.hasOwn(attachedInit, "spec") ? attachedInit.spec : spec;
  const sessionInit = Object.hasOwn(init, "session") ? init.session : observedHostedSession(threadId);
  const sessionPlacement =
    sessionInit?.placement !== undefined
      ? create(SessionPlacementSchema, sessionInit.placement)
      : specInit?.placement !== undefined
        ? create(SessionPlacementSchema, specInit.placement)
        : spec.placement;
  const attached =
    attachedInit === undefined
      ? undefined
      : create(AttachedSchema, {
          sessionId: threadId,
          lastCursor: 1n,
          harnessState: HarnessState.RUNNING,
          activeTurnId: "",
          setupState: SetupState.NOT_REQUIRED,
          launchOutcome: LaunchOutcome.STARTED,
          ...attachedInit,
          spec: specInit === undefined ? undefined : create(SessionSpecSchema, { ...spec, ...specInit }),
        });
  const defaultHost =
    sessionPlacement.kind.case === "centralSamplerV1"
      ? {
          case: "centralSamplerV1" as const,
          value: { setup: SetupState.NOT_REQUIRED },
        }
      : {
          case: "environmentHosted" as const,
          value: {
            setup: SetupState.NOT_REQUIRED,
            harness: { state: HarnessAttachment_State.ATTACHED },
          },
        };
  const defaultActivity = create(SessionActivitySchema, {
    kind: {
      case: "observed",
      value: create(HarnessSessionSchema, {
        sessionId: threadId,
        host: defaultHost,
      }),
    },
  });
  const sessionStatus =
    sessionInit === undefined
      ? undefined
      : create(SessionStatusSchema, {
          materialization:
            sessionInit.status?.materialization ?? SessionStatus_Materialization.SESSION_MATERIALIZATION_ACCEPTED,
          requiredEnvironment: sessionInit.status?.requiredEnvironment ?? SessionStatus_RequiredEnvironment.AVAILABLE,
          reason: sessionInit.status?.reason,
          deliveryBlocker: sessionInit.status?.deliveryBlocker,
          wakeAssessment: sessionInit.status?.wakeAssessment,
          activity:
            sessionInit.status !== undefined && Object.hasOwn(sessionInit.status, "activity")
              ? sessionInit.status.activity
              : defaultActivity,
        });
  const session =
    sessionInit === undefined
      ? undefined
      : create(SessionSchema, {
          sessionId: sessionInit.sessionId ?? threadId,
          placement: sessionPlacement,
          status: sessionStatus,
          createdAt: sessionInit.createdAt,
          updatedAt: sessionInit.updatedAt,
          identity: sessionInit.identity,
        });
  const summaryInit = {
    sessionId: threadId,
    foldedThrough: 1n,
    feedStatus: FeedStatus.ACTIVE,
    ...summary,
    sandbox: sandboxInit === undefined ? undefined : create(SandboxRefSchema, sandboxInit),
    attached,
  };
  return {
    metadata: create(ThreadMetadataSchema, {
      threadId,
      createdAt: timestampFromDate(new Date("2026-01-01T00:00:00Z")),
      archived: false,
      ...metadata,
    }),
    summary: create(SessionSummarySchema, summaryInit),
    session,
  };
}

export const THREAD: ThreadView = threadView({
  metadata: { name: "Test thread" },
  summary: { sandbox: { name: "composer-test", uid: "20000000-0000-4000-8000-000000000001" } },
});

export function viewState({
  harness = "running",
  status = ThreadFeedStatus.ACTIVE,
  model = "test-model",
  activeTurn = null,
}: {
  harness?: string | null;
  status?: ThreadFeedStatus;
  model?: string | null;
  activeTurn?: string | null;
} = {}): ThreadEntity {
  return create(ThreadEntitySchema, {
    sessionId: THREAD.metadata.threadId,
    projectionEpoch: "test-epoch",
    entityId: "current",
    cursor: 1n,
    revisionCursor: 1n,
    entity: {
      case: "viewState",
      value: {
        controls: {
          appliedModel: model ?? undefined,
          activeTurnId: activeTurn ?? undefined,
          harnessState: harness ?? undefined,
        },
        operational: {
          status,
          lastVerifiedCursor: 1n,
        },
      },
    },
  });
}

export function threadState({
  rows = [viewState()],
  caughtUp = true,
  reconnectingFor = null,
  windowError = null,
  error = null,
}: {
  rows?: ThreadEntity[];
  caughtUp?: boolean;
  /** How long the thread's reads have been failing, if they are. */
  reconnectingFor?: number | null;
  windowError?: string | null;
  error?: string | null;
} = {}): ThreadState {
  return {
    window: {
      rows,
      caughtUp,
      olderAvailable: false,
      loadingOlder: false,
      loadOlder: () => {},
      connection:
        reconnectingFor === null
          ? { phase: "live", since: Date.now() }
          : { phase: "reconnecting", since: Date.now() - reconnectingFor, attempt: 1, lastError: "HTTP 503" },
      error: windowError,
      retry: () => {},
    },
    error,
  };
}

export function reference(ownerId: string, field: ThreadPayloadField): ThreadPayloadReference {
  return create(ThreadPayloadReferenceSchema, {
    projectionEpoch: "test-epoch",
    ownerCursor: 1n,
    ownerId,
    field,
    revisionCursor: 1n,
    generation: 1n,
    chunkCount: 1n,
  });
}

export function entity(value: MessageInitShape<typeof ThreadEntitySchema>["entity"]): ThreadEntity {
  return create(ThreadEntitySchema, {
    sessionId: "test-thread",
    projectionEpoch: "test-epoch",
    entityId: "test-entity",
    entityIndex: 1n,
    cursor: 1n,
    revisionCursor: 1n,
    entity: value,
  });
}

/** Serves every body at once, over an empty thread with no command rows, except the `stopped` ones
 * (by `owner:field`), whose reads failed before any of them arrived and which `retry` reads again. */
export function serving(
  bodies: ReadonlyMap<string, string>,
  stopped: ReadonlyMap<string, () => void> = new Map(),
  loading: ReadonlySet<string> = new Set()
): ThreadSync {
  const empty = threadState({ rows: [] });
  return {
    Thread: ({ children }) => <>{children}</>,
    useThread: () => empty,
    useCommandRows: () => [],
    usePayload: ({ ownerId, field }) => {
      const storedField = ThreadPayloadField[field].toLowerCase();
      const key = `${ownerId}:${storedField}`;
      const retry = stopped.get(key);
      if (retry) return { body: null, error: "test read failure", retry };
      if (loading.has(key)) return { body: null, error: null, retry: () => {} };
      const body = bodies.get(key);
      if (body === undefined) throw new Error(`test fixture has no ${storedField} body for ${ownerId}`);
      return { body, error: null, retry: () => {} };
    },
  };
}

/** The status badges within `container`, by label. */
export function badgeLabels(container: Element): string[] {
  return [...container.querySelectorAll(".mantine-Badge-root")].map((badge) => badge.getAttribute("aria-label") ?? "");
}

export async function toggle(summary: Element): Promise<void> {
  await act(async () => (summary as HTMLButtonElement).click());
}
