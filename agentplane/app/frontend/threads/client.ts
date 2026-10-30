import { fromJson, type JsonValue } from "@bufbuild/protobuf";

import type {
  EvidencePage,
  NativeFramePage,
  ObservationEntry,
  ObservationPage,
  ThreadView as ThreadViewSchema,
} from "../schema";
import { api, displayableError, fetchWithLogin, ok } from "../client";
import { EventEntrySchema, type EventEntry } from "../../../proto/event_log_pb";
import {
  DispatchSchema,
  InterruptTurnResponseSchema,
  type Dispatch,
  type InterruptTurnResponse,
} from "../../../runner_gateway/dispatcher/session_dispatcher_pb";

type ThreadView = ThreadViewSchema;
export type ThreadScope = { projection_epoch: string; through_cursor: string };
export type ThreadPayloadRef = {
  projection_epoch: string;
  owner_cursor: string;
  owner_id: string;
  field: "text" | "arguments" | "output" | "confirmed_input" | "command_input";
  revision_cursor: string;
  generation: string;
  chunk_count: string;
};
export type ThreadEntityState =
  | {
      controls: {
        applied_model: string | null;
        applied_reasoning_effort: string | null;
        active_turn_id: string | null;
        harness_state: string | null;
        blocked_reason: string | null;
      };
      operational: {
        status: "active" | "ended" | "failed";
        last_verified_cursor: string;
        feed_error: { cursor: string | null; message: string } | null;
      };
    }
  | {
      kind: number;
      tool_name: string;
      completion: string | null;
      tool_succeeded: boolean | null;
      recovery: number | null;
      recovery_reason: string | null;
    }
  | { harness_message_id: string; origin_command_ids: string[] }
  | { observation: string; event: unknown }
  | {
      operation: string;
      outcome: "pending" | "effected" | "failed" | "noop";
      outcome_cursor: string | null;
      outcome_reason: string | null;
      requested_value: string | null;
    };
export type ThreadEntityView = {
  text_ref: ThreadPayloadRef | null;
  state: ThreadEntityState;
};
export async function threadObservationEntry(
  threadId: string,
  cursor: string,
  signal?: AbortSignal
): Promise<ObservationEntry> {
  return ok(
    await api.GET("/threads/{thread_id}/observations/{cursor}", {
      // openapi-fetch serializes path values without converting them to JS numbers.
      params: { path: { thread_id: threadId, cursor: cursor as unknown as number } },
      signal,
    })
  );
}

export async function threadObservations(
  threadId: string,
  cursor: { before?: string; after?: string },
  signal?: AbortSignal
): Promise<ObservationPage> {
  return ok(
    await api.GET("/threads/{thread_id}/observations", {
      params: {
        path: { thread_id: threadId },
        query: { before_cursor: cursor.before, after_cursor: cursor.after, limit: 30 },
      },
      signal,
    })
  );
}

export async function threadEvidence(
  threadId: string,
  scope: { projectionEpoch: string; entityKind: string; entityId: string },
  afterCursor = "0",
  signal?: AbortSignal
): Promise<EvidencePage> {
  return ok(
    await api.GET("/threads/{thread_id}/evidence", {
      params: {
        path: { thread_id: threadId },
        query: {
          projection_epoch: scope.projectionEpoch,
          entity_kind: scope.entityKind,
          entity_id: scope.entityId,
          after_cursor: afterCursor,
          limit: 30,
        },
      },
      signal,
    })
  );
}

export async function threadNativeFrames(
  threadId: string,
  scope: { projectionEpoch: string; entityKind: string; entityId: string },
  observationCursor: string,
  afterSequence = "0",
  signal?: AbortSignal
): Promise<NativeFramePage> {
  return ok(
    await api.GET("/threads/{thread_id}/evidence/{observation_cursor}/frames", {
      params: {
        path: { thread_id: threadId, observation_cursor: observationCursor },
        query: {
          projection_epoch: scope.projectionEpoch,
          entity_kind: scope.entityKind,
          entity_id: scope.entityId,
          after_sequence: afterSequence,
          limit: 30,
        },
      },
      signal,
    })
  );
}

/** An explicit 4xx command rejection: unlike a lost reply, this is not an admission receipt. */
export class CommandSubmissionRefused extends Error {}

export type CommandSubmission =
  | { kind: "archived_admission"; admission: EventEntry }
  | { kind: "dispatch"; dispatch: Dispatch }
  | { kind: "interrupt_result"; result: InterruptTurnResponse };

export function dispatchFromJson(value: unknown): Dispatch {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Session Dispatcher returned an invalid Dispatch");
  }
  const row = value as Record<string, unknown>;
  if (typeof row.commandId !== "string" || typeof row.sessionId !== "string" || typeof row.state !== "string") {
    throw new Error("Session Dispatcher returned a Dispatch without its command, Session or state");
  }
  return fromJson(DispatchSchema, value as JsonValue);
}

function interruptTurnResponse(value: unknown): InterruptTurnResponse {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Session Dispatcher returned an invalid interrupt result");
  }
  const row = value as Record<string, unknown>;
  if (typeof row.state !== "string") {
    throw new Error("Session Dispatcher returned an invalid interrupt state");
  }
  return fromJson(InterruptTurnResponseSchema, value as JsonValue);
}

/** The runner's exact durable CommandAdmitted receipt; the app archive may still be catching up.
 * Replaying the same immutable command id/payload is safe after a lost response.
 */
async function commandSubmission(
  call: () => Promise<{ data?: unknown; error?: unknown; response: Response }>
): Promise<CommandSubmission> {
  let result;
  try {
    result = await call();
  } catch (cause) {
    // A transport failure gives no proof that the app or runner did not admit the command.
    throw new Error(`Command admission unconfirmed: ${displayableError(cause)}`, { cause });
  }
  const { data, error, response } = result;
  if (error) {
    const message = displayableError(error);
    // A lost server reply, timeout or transient error can happen after runner admission.
    // Treat only an explicit, non-timeout client rejection as a refusal.
    if (response.status >= 400 && response.status < 500 && response.status !== 408) {
      throw new CommandSubmissionRefused(message);
    }
    throw new Error(`Command admission unconfirmed: ${message}`);
  }
  try {
    const value = data as Record<string, unknown>;
    if (value.kind === "dispatch") return { kind: "dispatch", dispatch: dispatchFromJson(value.dispatch) };
    if (value.kind === "interrupt_result")
      return { kind: "interrupt_result", result: interruptTurnResponse(value.result) };
    if (value.kind === "archived_admission")
      return { kind: "archived_admission", admission: fromJson(EventEntrySchema, value.admission as JsonValue) };
    throw new Error("App returned an unrecognized command submission kind");
  } catch (cause) {
    throw new Error(`Command receipt could not be verified: ${displayableError(cause)}`, { cause });
  }
}

export async function submitInput(threadId: string, commandId: string, text: string): Promise<CommandSubmission> {
  return commandSubmission(() =>
    api.POST("/threads/{thread_id}/submit-input", {
      params: { path: { thread_id: threadId } },
      body: { commandId, text },
    })
  );
}

export async function changeModel(threadId: string, commandId: string, model: string): Promise<CommandSubmission> {
  return commandSubmission(() =>
    api.POST("/threads/{thread_id}/change-model", {
      params: { path: { thread_id: threadId } },
      body: { commandId, model },
    })
  );
}

export async function changeReasoningEffort(
  threadId: string,
  commandId: string,
  effort: string
): Promise<CommandSubmission> {
  return commandSubmission(() =>
    api.POST("/threads/{thread_id}/change-reasoning-effort", {
      params: { path: { thread_id: threadId } },
      body: { commandId, effort },
    })
  );
}

export async function interruptTurn(threadId: string, commandId: string, turnId: string): Promise<CommandSubmission> {
  return commandSubmission(() =>
    api.POST("/threads/{thread_id}/interrupt-turn", {
      params: { path: { thread_id: threadId } },
      body: { commandId, turnId },
    })
  );
}

/**
 * The epoch a thread's shapes are pinned to. A long poll: the server holds the read until the
 * thread has a fold, and answers null if it still has none when the hold runs out.
 */
export async function threadScope(threadId: string, signal?: AbortSignal): Promise<ThreadScope | null> {
  const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/scope`, window.location.href);
  const response = await fetchWithLogin(url, { signal });
  if (response.status === 204) return null;
  if (!response.ok) throw new Error(`Thread scope failed with ${response.status}`);
  return (await response.json()) as ThreadScope;
}

export async function getThread(threadId: string): Promise<ThreadView> {
  const thread = await findThread(threadId);
  if (!thread) throw new Error(`Thread ${threadId} not found`);
  return thread;
}

/** null until the History Service holds an event of the Thread's Session. */
export async function findThread(threadId: string): Promise<ThreadView | null> {
  const answer = await api.GET("/threads/{thread_id}", { params: { path: { thread_id: threadId } } });
  return answer.response.status === 404 ? null : ok(answer);
}

/** Set the thread's name; null (or a blank, which the API normalises) leaves it unnamed. */
export async function renameThread(threadId: string, name: string | null): Promise<ThreadView> {
  return ok(
    await api.PATCH("/threads/{thread_id}", {
      params: { path: { thread_id: threadId } },
      body: { name },
    })
  );
}

export async function archiveThread(threadId: string, archived: boolean): Promise<void> {
  ok(
    await api.POST(`/threads/{thread_id}/${archived ? "archive" : "unarchive"}`, {
      params: { path: { thread_id: threadId } },
    })
  );
}
