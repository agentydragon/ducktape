import { create, fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";

import type {
  EvidencePage,
  NativeFramePage,
  ObservationEntry,
  ObservationPage,
  ThreadView as ThreadViewSchema,
} from "../schema";
import { api, displayableError, fetchWithLogin, HttpError, ok } from "../client";
import { EventEntrySchema, type EventEntry } from "../../../proto/event_log_pb";
import {
  EntityKeySchema,
  ReadEntityEvidenceRequestSchema,
  ReadEntityEvidenceResponseSchema,
  ReadNativeFramesRequestSchema,
  ReadNativeFramesResponseSchema,
  ReadSessionEventsRequestSchema,
  ReadSessionEventsResponseSchema,
  ReadSessionObservationsRequestSchema,
  ReadSessionObservationsResponseSchema,
  GetSessionSummaryRequestSchema,
  SessionSummarySchema,
  type ReadSessionObservationsResponse,
} from "../../../history_service/history_service_pb";
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

async function historyJson(path: string, init: RequestInit = {}): Promise<JsonValue> {
  const response = await fetchWithLogin(new URL(`/upstream/history/v1${path}`, window.location.href), {
    ...init,
    headers: { Accept: "application/json", ...init.headers },
  });
  if (!response.ok) {
    throw new HttpError(response, await response.json().catch(() => null));
  }
  return (await response.json()) as JsonValue;
}

function historyBodyWithoutSessionId(value: JsonValue): string {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("History request did not serialize to a JSON object");
  }
  const body = { ...value };
  delete body.sessionId;
  return JSON.stringify(body);
}

function nextStored(cursor: bigint, ranges: ReadonlyArray<{ first: bigint; last: bigint }>): bigint {
  let following = cursor + 1n;
  for (const range of [...ranges].sort((left, right) =>
    left.first < right.first ? -1 : left.first > right.first ? 1 : 0
  )) {
    if (range.first <= following && following <= range.last) following = range.last + 1n;
  }
  return following;
}

function observationPage(response: ReadSessionObservationsResponse): ObservationPage {
  return {
    observations: response.observations.map((observation) => ({
      cursor: observation.cursor.toString(),
      kind: observation.kind,
    })),
    next_before_cursor:
      response.observations[0] && response.observations[0].cursor > 1n
        ? response.observations[0].cursor.toString()
        : null,
    next_after_cursor:
      response.observations.length > 0 &&
      nextStored(response.observations.at(-1)!.cursor, response.settled) <= response.lastCursor
        ? response.observations.at(-1)!.cursor.toString()
        : null,
  };
}

function validateObservationPage(
  response: ReadSessionObservationsResponse,
  before: bigint | undefined,
  after: bigint | undefined,
  limit: number
): void {
  const cursors = response.observations.map((observation) => observation.cursor);
  let walk = cursors;
  let within: boolean;
  let shortOk: boolean;
  if (after !== undefined) {
    if (after > response.lastCursor) throw new Error("History observation cursor exceeds the History watermark");
    walk = [after, ...cursors];
    const last = walk.at(-1)!;
    within = cursors.length === 0 || cursors.at(-1)! <= response.lastCursor;
    shortOk = nextStored(last, response.settled) > response.lastCursor;
  } else {
    let end = response.lastCursor;
    if (before !== undefined && before <= end) end = before === 0n ? 0n : before - 1n;
    within = cursors.length === 0 || (cursors.at(-1)! <= end && end < nextStored(cursors.at(-1)!, response.settled));
    shortOk = (cursors.length > 0 && cursors[0] === 1n) || (cursors.length === 0 && end < 1n);
  }
  let contiguous = true;
  for (let index = 1; index < walk.length; index++) {
    if (walk[index] !== nextStored(walk[index - 1]!, response.settled)) {
      contiguous = false;
      break;
    }
  }
  if (!within || !contiguous || (cursors.length !== limit && !shortOk)) {
    throw new Error("History Service returned an invalid observation page");
  }
}
export async function threadObservationEntry(
  threadId: string,
  cursor: string,
  signal?: AbortSignal
): Promise<ObservationEntry> {
  const requestedCursor = BigInt(cursor);
  if (requestedCursor < 1n) {
    throw new HttpError(new Response(null, { status: 404 }), `no observation at ${cursor} in this thread`);
  }

  const sessionId = encodeURIComponent(threadId);
  const summaryRequest = create(GetSessionSummaryRequestSchema, { sessionId: threadId });
  const summary = fromJson(
    SessionSummarySchema,
    await historyJson(`/sessions/${encodeURIComponent(summaryRequest.sessionId)}/summary`, { signal })
  );
  const watermarkUrl = new URL(`/sessions/${sessionId}/observations`, window.location.href);
  watermarkUrl.searchParams.set("limit", "1");
  const watermark = fromJson(
    ReadSessionObservationsResponseSchema,
    await historyJson(watermarkUrl.pathname + watermarkUrl.search, { signal })
  );
  if (requestedCursor > watermark.lastCursor) {
    if (requestedCursor <= summary.foldedThrough) {
      throw new Error(`History Service has not committed cursor ${cursor} used by its fold`);
    }
    throw new HttpError(new Response(null, { status: 404 }), `no observation at ${cursor} in this thread`);
  }

  const eventsRequest = create(ReadSessionEventsRequestSchema, {
    sessionId: threadId,
    afterCursor: requestedCursor - 1n,
    limit: 1,
  });
  const eventsUrl = new URL(`/sessions/${sessionId}/events`, window.location.href);
  eventsUrl.searchParams.set("after_cursor", eventsRequest.afterCursor.toString());
  eventsUrl.searchParams.set("limit", String(eventsRequest.limit));
  const page = fromJson(
    ReadSessionEventsResponseSchema,
    await historyJson(eventsUrl.pathname + eventsUrl.search, { signal })
  );
  const unstored = [...page.settlements.flatMap((settlement) => settlement.ranges), ...page.removed];
  if (nextStored(requestedCursor - 1n, unstored) !== requestedCursor) {
    throw new HttpError(new Response(null, { status: 404 }), `no observation at ${cursor} in this thread`);
  }
  if (page.entries.length !== 1 || page.entries[0]!.cursor !== requestedCursor) {
    throw new Error(`History Service did not return requested cursor ${cursor}`);
  }
  return {
    cursor: requestedCursor.toString(),
    entry: toJson(EventEntrySchema, page.entries[0]!) as ObservationEntry["entry"],
  };
}

export async function threadObservations(
  threadId: string,
  cursor: { before?: string; after?: string },
  signal?: AbortSignal
): Promise<ObservationPage> {
  const request = create(ReadSessionObservationsRequestSchema, {
    sessionId: threadId,
    beforeCursor: cursor.before === undefined ? undefined : BigInt(cursor.before),
    afterCursor: cursor.after === undefined ? undefined : BigInt(cursor.after),
    limit: 30,
  });
  const url = new URL(`/sessions/${encodeURIComponent(threadId)}/observations`, window.location.href);
  url.searchParams.set("limit", String(request.limit));
  if (request.beforeCursor !== undefined) url.searchParams.set("before_cursor", request.beforeCursor.toString());
  if (request.afterCursor !== undefined) url.searchParams.set("after_cursor", request.afterCursor.toString());
  const response = fromJson(
    ReadSessionObservationsResponseSchema,
    await historyJson(url.pathname + url.search, { signal })
  );
  validateObservationPage(
    response,
    cursor.before === undefined ? undefined : BigInt(cursor.before),
    cursor.after === undefined ? undefined : BigInt(cursor.after),
    30
  );
  return observationPage(response);
}

export async function threadEvidence(
  threadId: string,
  scope: { projectionEpoch: string; entityKind: string; entityId: string },
  afterCursor = "0",
  signal?: AbortSignal
): Promise<EvidencePage> {
  const request = create(ReadEntityEvidenceRequestSchema, {
    sessionId: threadId,
    projectionEpoch: scope.projectionEpoch,
    entity: create(EntityKeySchema, { entityKind: scope.entityKind, entityId: scope.entityId }),
    afterCursor: BigInt(afterCursor),
    limit: 30,
  });
  const response = fromJson(
    ReadEntityEvidenceResponseSchema,
    await historyJson(`/sessions/${encodeURIComponent(threadId)}/entity-evidence`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: historyBodyWithoutSessionId(toJson(ReadEntityEvidenceRequestSchema, request)),
      signal,
    })
  );
  return {
    observations: response.observations.map((observation) => ({
      observation_cursor: observation.observationCursor.toString(),
      has_native: observation.hasNative,
    })),
    next_after_cursor: response.nextAfterCursor?.toString() ?? null,
  };
}

export async function threadNativeFrames(
  threadId: string,
  scope: { projectionEpoch: string; entityKind: string; entityId: string },
  observationCursor: string,
  afterSequence = "0",
  signal?: AbortSignal
): Promise<NativeFramePage> {
  const request = create(ReadNativeFramesRequestSchema, {
    sessionId: threadId,
    projectionEpoch: scope.projectionEpoch,
    entity: create(EntityKeySchema, { entityKind: scope.entityKind, entityId: scope.entityId }),
    observationCursor: BigInt(observationCursor),
    afterSequence: BigInt(afterSequence),
    limit: 30,
  });
  const response = fromJson(
    ReadNativeFramesResponseSchema,
    await historyJson(`/sessions/${encodeURIComponent(threadId)}/native-frames`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: historyBodyWithoutSessionId(toJson(ReadNativeFramesRequestSchema, request)),
      signal,
    })
  );
  return {
    frames: response.frames.map((frame) => ({
      source_sequence: frame.sourceSequence.toString(),
      availability: frame.entry ? "present" : "unavailable",
      entry: frame.entry ? (toJson(EventEntrySchema, frame.entry) as Record<string, unknown>) : {},
    })),
    next_after_sequence: response.nextAfterSequence?.toString() ?? null,
  };
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
