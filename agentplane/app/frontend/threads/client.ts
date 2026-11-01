import { create, fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";
import { TimestampSchema, type Timestamp } from "@bufbuild/protobuf/wkt";

import type { ThreadView as ThreadViewSchema } from "../schema";
import { api, displayableError, fetchWithLogin, HttpError, ok } from "../client";
import { EventEntrySchema, type EventEntry } from "../../../proto/event_log_pb";
import { TurnStatusSchema } from "../../../proto/event_pb";
import { HarnessSchema, HarnessStateSchema, LaunchOutcomeSchema, SetupStateSchema } from "../../../proto/runner_pb";
import {
  ListThreadMetadataResponseSchema,
  ThreadMetadataSchema,
  type ThreadMetadata,
} from "../../thread_metadata/thread_metadata_pb";
import {
  BatchGetSessionSummariesRequestSchema,
  BatchGetSessionSummariesResponseSchema,
  EntityKeySchema,
  FeedStatusSchema,
  FindSessionsResponseSchema,
  GetSessionSummaryRequestSchema,
  ReadEntityEvidenceRequestSchema,
  ReadEntityEvidenceResponseSchema,
  ReadNativeFramesRequestSchema,
  ReadNativeFramesResponseSchema,
  ReadSessionEventsRequestSchema,
  ReadSessionEventsResponseSchema,
  ReadSessionObservationsRequestSchema,
  ReadSessionObservationsResponseSchema,
  SessionSummarySchema,
  type SessionSummary,
  type ReadSessionObservationsResponse,
} from "../../../history_service/history_service_pb";
import {
  ChangeModelRequestSchema,
  ChangeModelResponseSchema,
  ChangeReasoningEffortRequestSchema,
  ChangeReasoningEffortResponseSchema,
  DeliveryState,
  DispatchSchema,
  InterruptTurnRequestSchema,
  InterruptTurnResponseSchema,
  SubmitInputRequestSchema,
  SubmitInputResponseSchema,
  type Dispatch,
  type InterruptTurnResponse,
} from "../../../runner_gateway/dispatcher/session_dispatcher_pb";

export type CommandTarget = { sandbox: string; sandboxUid: string };

type ThreadView = ThreadViewSchema;
export interface ThreadListing {
  limit: number;
  search: string | null;
  includeArchived: boolean;
}
export type ThreadWindow = { threads: ThreadView[]; more: boolean; archived_count: number };
export type ObservationEntry = { cursor: string; entry: JsonValue };
export type ObservationPage = {
  observations: Array<{ cursor: string; kind: string }>;
  next_before_cursor: string | null;
  next_after_cursor: string | null;
};
export type EvidencePage = {
  observations: Array<{ observation_cursor: string; has_native: boolean }>;
  next_after_cursor: string | null;
};
export type NativeFramePage = {
  frames: Array<{
    source_sequence: string;
    availability: "present" | "unavailable";
    entry: JsonValue;
  }>;
  next_after_sequence: string | null;
};
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

function bodyWithoutSessionId(value: JsonValue): string {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Request did not serialize to a JSON object");
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
    shortOk =
      cursors.length > 0 ? nextStored(0n, response.settled) === cursors[0] : nextStored(0n, response.settled) > end;
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
      body: bodyWithoutSessionId(toJson(ReadEntityEvidenceRequestSchema, request)),
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
      body: bodyWithoutSessionId(toJson(ReadNativeFramesRequestSchema, request)),
      signal,
    })
  );
  return {
    frames: response.frames.map((frame) => ({
      source_sequence: frame.sourceSequence.toString(),
      availability: frame.entry ? "present" : "unavailable",
      entry: frame.entry ? toJson(EventEntrySchema, frame.entry) : {},
    })),
    next_after_sequence: response.nextAfterSequence?.toString() ?? null,
  };
}

/** An explicit Dispatcher rejection of this request, not a durable admission receipt. */
export class CommandSubmissionRefused extends Error {}

export type CommandSubmission =
  { kind: "dispatch"; dispatch: Dispatch } | { kind: "interrupt_result"; result: InterruptTurnResponse };

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

/** A timeout, lost response, stale destination, or missing Sandbox does not prove an earlier
 * attempt was never admitted. Only invariant/auth rejections are known refusals. */
async function dispatcherCommand(path: string, body: string): Promise<JsonValue> {
  let response: Response;
  try {
    response = await fetchWithLogin(new URL(`/upstream/dispatcher/v1${path}`, window.location.href), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body,
    });
  } catch (cause) {
    throw new Error(`Command admission unconfirmed: ${displayableError(cause)}`, { cause });
  }
  const value = (await response.json().catch(() => null)) as JsonValue;
  if (!response.ok) {
    const error = new HttpError(response, value);
    const code = value && typeof value === "object" && !Array.isArray(value) ? value.code : undefined;
    if (code === 3 || code === 6 || code === 7 || code === 16 || (response.status === 403 && code === undefined)) {
      throw new CommandSubmissionRefused(error.message);
    }
    throw new Error(`Command admission unconfirmed: ${error.message}`);
  }
  if (value === null) throw new Error("Command receipt could not be verified: empty Dispatcher response");
  return value;
}

function confirmedDispatch(
  dispatch: Dispatch | undefined,
  threadId: string,
  target: CommandTarget,
  commandId: string
): Dispatch {
  if (!dispatch) throw new Error("Command receipt could not be verified: missing Dispatch");
  if (
    dispatch.commandId !== commandId ||
    dispatch.sessionId !== threadId ||
    dispatch.sandbox !== target.sandbox ||
    dispatch.sandboxUid !== target.sandboxUid ||
    dispatch.state === DeliveryState.UNSPECIFIED
  )
    throw new Error("Command receipt could not be verified: mismatched Dispatcher destination or command");
  return dispatch;
}

export async function submitInput(
  threadId: string,
  target: CommandTarget,
  commandId: string,
  text: string
): Promise<CommandSubmission> {
  const request = create(SubmitInputRequestSchema, { sessionId: threadId, commandId, ...target, text });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:submitInput`,
    bodyWithoutSessionId(toJson(SubmitInputRequestSchema, request))
  );
  const response = fromJson(SubmitInputResponseSchema, value);
  return { kind: "dispatch", dispatch: confirmedDispatch(response.dispatch, threadId, target, commandId) };
}

export async function changeModel(
  threadId: string,
  target: CommandTarget,
  commandId: string,
  model: string
): Promise<CommandSubmission> {
  const request = create(ChangeModelRequestSchema, { sessionId: threadId, commandId, ...target, model });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:changeModel`,
    bodyWithoutSessionId(toJson(ChangeModelRequestSchema, request))
  );
  const response = fromJson(ChangeModelResponseSchema, value);
  return { kind: "dispatch", dispatch: confirmedDispatch(response.dispatch, threadId, target, commandId) };
}

export async function changeReasoningEffort(
  threadId: string,
  target: CommandTarget,
  commandId: string,
  effort: string
): Promise<CommandSubmission> {
  const request = create(ChangeReasoningEffortRequestSchema, { sessionId: threadId, commandId, ...target, effort });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:changeReasoningEffort`,
    bodyWithoutSessionId(toJson(ChangeReasoningEffortRequestSchema, request))
  );
  const response = fromJson(ChangeReasoningEffortResponseSchema, value);
  return { kind: "dispatch", dispatch: confirmedDispatch(response.dispatch, threadId, target, commandId) };
}

export async function interruptTurn(
  threadId: string,
  target: CommandTarget,
  commandId: string,
  turnId: string
): Promise<CommandSubmission> {
  const request = create(InterruptTurnRequestSchema, { sessionId: threadId, commandId, ...target, turnId });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:interruptTurn`,
    bodyWithoutSessionId(toJson(InterruptTurnRequestSchema, request))
  );
  const result = fromJson(InterruptTurnResponseSchema, value);
  if (result.state !== DeliveryState.RUNNER_ADMITTED && result.state !== DeliveryState.NO_OP) {
    throw new Error("Command receipt could not be verified: invalid interrupt result");
  }
  return { kind: "interrupt_result", result };
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

/** Join app-owned metadata with History's generated Session summary at the browser boundary. */
export async function findThread(threadId: string, signal?: AbortSignal): Promise<ThreadView | null> {
  const encodedId = encodeURIComponent(threadId);
  const [metadata, summary] = await Promise.all([
    fetchProto(
      `/upstream/app/v1/threads/${encodedId}`,
      (json) => fromJson(ThreadMetadataSchema, json),
      `Thread metadata for ${threadId}`,
      signal
    ),
    fetchProto(
      `/upstream/history/v1/sessions/${encodedId}/summary`,
      (json) => fromJson(SessionSummarySchema, json),
      `History summary for ${threadId}`,
      signal
    ),
  ]);
  if (!metadata || !summary || !summary.sandbox?.name) return null;
  return composeThreadView(metadata, summary);
}

async function fetchProto<T>(
  path: string,
  parse: (json: JsonValue) => T,
  label: string,
  signal?: AbortSignal
): Promise<T | null> {
  const response = await fetchWithLogin(new URL(path, window.location.href), { signal });
  if (response.status === 404) return null;
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = displayableError(await response.json());
    } catch {
      // Keep the HTTP status when the gateway returned a non-JSON error body.
    }
    throw new Error(`${label} failed with ${response.status}: ${detail}`);
  }
  return parse((await response.json()) as JsonValue);
}

function timestampISO(value: Timestamp | undefined): string | null {
  if (!value) return null;
  const json = toJson(TimestampSchema, value);
  if (typeof json !== "string") throw new Error("History Service returned an invalid timestamp");
  return json;
}

function enumName(
  value: number,
  schema: { values: readonly { number: number; name: string }[] },
  label: string
): string {
  const name = schema.values.find((member) => member.number === value)?.name;
  if (!name) throw new Error(`History Service returned an unknown ${label}: ${value}`);
  return name;
}

function composeThreadView(metadata: ThreadMetadata, summary: SessionSummary): ThreadView {
  const attachment = summary.attached;
  const spec = attachment?.spec;
  const sandboxUID = summary.sandbox?.uid;
  const createdAt = timestampISO(metadata.createdAt);
  if (!createdAt) throw new Error("App returned Thread metadata without created_at");
  const feedStatusNames: Record<string, string> = {
    FEED_STATUS_ACTIVE: "active",
    FEED_STATUS_ENDED: "ended",
    FEED_STATUS_FAILED: "failed",
  };
  // An attached Session can precede the first runner feed observation. History represents
  // that valid state with FEED_STATUS_UNSPECIFIED, which the old Thread view exposed as null.
  const feedStatusName =
    attachment && summary.feedStatus !== 0 ? enumName(summary.feedStatus, FeedStatusSchema, "FeedStatus") : null;
  const feedStatus = feedStatusName === null ? null : feedStatusNames[feedStatusName];
  if (feedStatusName !== null && !feedStatus) {
    throw new Error(`History Service returned an unknown FeedStatus: ${feedStatusName}`);
  }

  return {
    id: metadata.threadId,
    sandbox: summary.sandbox?.name ?? "",
    sandbox_uid: sandboxUID || null,
    harness: spec ? enumName(spec.harness, HarnessSchema, "Harness") : null,
    model: spec?.model ?? null,
    cwd: spec?.cwd ?? null,
    created_at: createdAt,
    name: metadata.name ?? null,
    archived: metadata.archived,
    last_cursor: Number(summary.foldedThrough),
    last_event_at: timestampISO(summary.lastEventAt),
    last_model_activity_at: timestampISO(summary.lastModelActivityAt),
    idle_since: timestampISO(summary.idleSince),
    harness_state: enumName(attachment?.harnessState ?? 0, HarnessStateSchema, "HarnessState"),
    setup_state: attachment ? enumName(attachment.setupState, SetupStateSchema, "SetupState") : null,
    launch_outcome: attachment ? enumName(attachment.launchOutcome, LaunchOutcomeSchema, "LaunchOutcome") : null,
    active_turn_id: attachment?.activeTurnId || null,
    reasoning_effort: spec?.reasoningEffort ?? null,
    feed_status: feedStatus ?? null,
    last_turn_status:
      summary.lastTurnStatus === undefined ? null : enumName(summary.lastTurnStatus, TurnStatusSchema, "TurnStatus"),
  } as ThreadView;
}

/** Pages app-owned Thread rows, joins History summaries, and keeps the former live-list semantics. */
export async function listThreadWindow(
  listing: ThreadListing,
  openThreadId: string | null,
  signal?: AbortSignal
): Promise<ThreadWindow> {
  if (listing.limit < 1) throw new Error("Thread list limit must be positive");

  const search = listing.search?.toLowerCase() ?? null;
  const sandboxMatches = new Set<string>();
  if (listing.search !== null) {
    const query = new URLSearchParams({ sandbox_name_contains: listing.search });
    const found = fromJson(FindSessionsResponseSchema, await historyJson(`/sessions:find?${query}`, { signal }));
    for (const session of found.sessions) sandboxMatches.add(session.sessionId);
  }

  const threads: ThreadView[] = [];
  const seenTokens = new Set<string>();
  let archivedCount = 0;
  let pageToken = "";
  let more = false;
  for (;;) {
    const query = new URLSearchParams({ include_archived: String(listing.includeArchived), limit: "200" });
    if (pageToken !== "") query.set("page_token", pageToken);
    const metadata = await fetchProto(
      `/upstream/app/v1/threads?${query}`,
      (json) => fromJson(ListThreadMetadataResponseSchema, json),
      "Thread metadata list",
      signal
    );
    if (!metadata) throw new Error("Thread metadata list was not found");
    if (pageToken === "") archivedCount = metadata.archivedCount;

    const ids = metadata.threads.map((thread) => thread.threadId);
    const summaries =
      ids.length === 0
        ? []
        : fromJson(
            BatchGetSessionSummariesResponseSchema,
            await historyJson("/sessions:batchGetSummaries", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(
                toJson(
                  BatchGetSessionSummariesRequestSchema,
                  create(BatchGetSessionSummariesRequestSchema, { sessionIds: ids })
                )
              ),
              signal,
            })
          ).summaries;
    const byID = new Map(summaries.map((summary) => [summary.sessionId, summary]));
    for (const metadataRow of metadata.threads) {
      const summary = byID.get(metadataRow.threadId);
      if (!summary) continue;
      const matchesSearch =
        search === null ||
        (metadataRow.name?.toLowerCase().includes(search) ?? false) ||
        metadataRow.threadId.toLowerCase().startsWith(search) ||
        sandboxMatches.has(metadataRow.threadId);
      if (!matchesSearch) continue;
      if (!summary.sandbox?.name) throw new Error("History Session has no Sandbox identity");
      threads.push(composeThreadView(metadataRow, summary));
      if (threads.length > listing.limit) {
        more = true;
        break;
      }
    }
    if (more || metadata.nextPageToken === "") break;
    if (seenTokens.has(metadata.nextPageToken) || metadata.nextPageToken === pageToken) {
      throw new Error("Thread metadata page token did not advance");
    }
    seenTokens.add(metadata.nextPageToken);
    pageToken = metadata.nextPageToken;
  }

  const visible = threads.slice(0, listing.limit);
  if (openThreadId !== null && !visible.some((thread) => thread.id === openThreadId)) {
    try {
      const opened = await findThread(openThreadId, signal);
      if (opened) visible.push(opened);
    } catch (reason) {
      if (signal?.aborted) throw reason;
    }
  }
  return { threads: visible, more, archived_count: archivedCount };
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
