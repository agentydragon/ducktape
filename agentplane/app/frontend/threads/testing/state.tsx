import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { act } from "react";

import {
  ThreadEntitySchema,
  ThreadFeedStatus,
  ThreadPayloadField,
  ThreadPayloadReferenceSchema,
  type ThreadEntity,
  type ThreadPayloadReference,
} from "../../../../history_service/history_service_pb";

import type { ThreadView } from "../client";
import type { ThreadState, ThreadSync } from "../thread_sync";

export const THREAD: ThreadView = {
  id: "10000000-0000-4000-8000-000000000001",
  sandbox: "composer-test",
  harness: "HARNESS_CLAUDE",
  model: "test-model",
  cwd: "/test-workspace",
  created_at: "2026-01-01T00:00:00Z",
  name: "Test thread",
  archived: false,
  last_cursor: 1,
  sandbox_uid: "20000000-0000-4000-8000-000000000001",
  last_event_at: null,
  last_model_activity_at: null,
  idle_since: null,
  harness_state: "HARNESS_STATE_RUNNING",
  setup_state: null,
  launch_outcome: null,
  active_turn_id: null,
  reasoning_effort: "low",
  feed_status: null,
  last_turn_status: null,
};

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
    sessionId: THREAD.id,
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
