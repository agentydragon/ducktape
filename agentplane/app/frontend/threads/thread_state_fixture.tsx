import { act } from "react";

import type { ThreadView } from "../client";
import type { PayloadRef, ThreadEntity, ThreadState, ThreadSync } from "./thread_sync";

export const THREAD: ThreadView = {
  id: "10000000-0000-4000-8000-000000000001",
  sandbox: "composer-test",
  session_id: "session-test",
  harness: "HARNESS_CLAUDE",
  model: "test-model",
  cwd: "/test-workspace",
  created_at: "2026-01-01T00:00:00Z",
  name: "Test thread",
  archived: false,
  last_cursor: 1,
  last_event_at: null,
  harness_state: "HARNESS_STATE_RUNNING",
  reasoning_effort: "low",
};

export function viewState({
  harness = "running",
  status = "active",
  model = "test-model",
  activeTurn = null,
}: {
  harness?: string | null;
  status?: "active" | "ended" | "failed";
  model?: string | null;
  activeTurn?: string | null;
} = {}): ThreadEntity {
  return {
    threadId: THREAD.id,
    projectionEpoch: "test-epoch",
    entityKind: "view_state",
    entityId: "current",
    entityIndex: "0",
    cursor: "1",
    revisionCursor: "1",
    pending: false,
    turnId: null,
    state: {
      controls: {
        applied_model: model,
        applied_reasoning_effort: null,
        active_turn_id: activeTurn,
        harness_state: harness,
      },
      operational: { status, last_verified_cursor: "1", feed_error: null },
    },
    textRef: null,
    argumentsRef: null,
    outputRef: null,
    inputRef: null,
  };
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
      refresh: () => {},
    },
    error,
  };
}

export function reference(ownerId: string, field: PayloadRef["field"]): PayloadRef {
  return {
    projection_epoch: "test-epoch",
    owner_cursor: "1",
    owner_id: ownerId,
    field,
    revision_cursor: "1",
    generation: "1",
    chunk_count: "1",
  };
}

export function entity(
  entityKind: ThreadEntity["entityKind"],
  state: ThreadEntity["state"],
  refs: Partial<Pick<ThreadEntity, "textRef" | "argumentsRef" | "outputRef" | "inputRef">>
): ThreadEntity {
  return {
    threadId: "test-thread",
    projectionEpoch: "test-epoch",
    entityKind,
    entityId: "test-entity",
    entityIndex: "1",
    cursor: "1",
    revisionCursor: "1",
    pending: false,
    turnId: null,
    state,
    textRef: null,
    argumentsRef: null,
    outputRef: null,
    inputRef: null,
    ...refs,
  };
}

/** Serves every body at once, over an empty thread with no command rows. */
export function serving(bodies: ReadonlyMap<string, string>): ThreadSync {
  const empty = threadState({ rows: [] });
  return {
    Thread: ({ children }) => <>{children}</>,
    useThread: () => empty,
    useCommandRows: () => [],
    usePayload: ({ owner_id, field }) => {
      const body = bodies.get(`${owner_id}:${field}`);
      if (body === undefined) throw new Error(`test fixture has no ${field} body for ${owner_id}`);
      return { body, error: null, retry: () => {} };
    },
  };
}

export async function toggle(summary: Element): Promise<void> {
  const details = summary.parentElement as HTMLDetailsElement;
  await act(async () => {
    details.open = !details.open;
    details.dispatchEvent(new Event("toggle"));
  });
}
