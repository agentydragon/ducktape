import { create, fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";

import { displayableError, fetchJson, fetchText, fetchWithLogin, HttpError } from "../client";
import { TurnStatusSchema } from "../../../proto/event_pb";
import {
  Harness,
  HarnessSchema,
  HarnessState,
  HarnessStateSchema,
  LaunchOutcomeSchema,
  SetupStateSchema,
} from "../../../proto/session_pb";
import {
  ListThreadMetadataResponseSchema,
  RenameThreadRequestSchema,
  ThreadMetadataTargetSchema,
  ThreadMetadataSchema,
  type ThreadMetadata,
} from "../../app_service_pb";
import {
  BatchGetSessionSummariesRequestSchema,
  BatchGetSessionSummariesResponseSchema,
  FeedStatus,
  FeedStatusSchema,
  FindSessionsResponseSchema,
  GetSessionSummaryRequestSchema,
  ReadEntityEvidenceRequestSchema,
  ReadEntityEvidenceResponseSchema,
  ReadNativeFramesRequestSchema,
  ReadNativeFramesResponseSchema,
  ReadThreadEntitiesRequestSchema,
  ReadThreadEntitiesResponseSchema,
  type ReadEntityEvidenceRequest,
  type ReadEntityEvidenceResponse,
  type ReadNativeFramesRequest,
  type ReadNativeFramesResponse,
  type ReadSessionEventsRequest,
  type ReadSessionEventsResponse,
  type ReadSessionObservationsRequest,
  ReadSessionEventsResponseSchema,
  ReadSessionObservationsResponseSchema,
  SessionSummarySchema,
  type SessionSummary,
  type ReadSessionObservationsResponse,
  type ReadThreadEntitiesResponse,
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
} from "../../../runner_gateway/dispatcher/session_dispatcher_pb";

export type CommandTarget = { sandbox: string; sandboxUid: string };

/** App metadata and its History projection stay in their generated wire shapes. */
export interface ThreadView {
  metadata: ThreadMetadata;
  summary: SessionSummary;
}
export interface ThreadListing {
  limit: number;
  search: string | null;
  includeArchived: boolean;
  sandboxUID?: string;
}
export type ThreadWindow = { threads: ThreadView[]; more: boolean; archived_count: number };
function historyJson(path: string, init: RequestInit = {}): Promise<JsonValue> {
  return fetchJson(new URL(`/upstream/history/v1${path}`, window.location.href), {
    ...init,
    headers: { Accept: "application/json", ...init.headers },
  });
}

function bodyWithoutSessionId(value: JsonValue): string {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Request did not serialize to a JSON object");
  }
  const body = { ...value };
  delete body.sessionId;
  return JSON.stringify(body);
}

export function nextStored(cursor: bigint, ranges: ReadonlyArray<{ first: bigint; last: bigint }>): bigint {
  let following = cursor + 1n;
  for (const range of [...ranges].sort((left, right) =>
    left.first < right.first ? -1 : left.first > right.first ? 1 : 0
  )) {
    if (range.first <= following && following <= range.last) following = range.last + 1n;
  }
  return following;
}

function validateObservationPage(
  response: ReadSessionObservationsResponse,
  request: ReadSessionObservationsRequest
): void {
  const cursors = response.observations.map((observation) => observation.cursor);
  let walk = cursors;
  let within: boolean;
  let shortOk: boolean;
  if (request.afterCursor !== undefined) {
    if (request.afterCursor > response.lastCursor)
      throw new Error("History observation cursor exceeds the History watermark");
    walk = [request.afterCursor, ...cursors];
    const last = walk.at(-1)!;
    within = cursors.length === 0 || cursors.at(-1)! <= response.lastCursor;
    shortOk = nextStored(last, response.settled) > response.lastCursor;
  } else {
    let end = response.lastCursor;
    if (request.beforeCursor !== undefined && request.beforeCursor <= end) {
      end = request.beforeCursor === 0n ? 0n : request.beforeCursor - 1n;
    }
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
  if (!within || !contiguous || (cursors.length !== request.limit && !shortOk)) {
    throw new Error("History Service returned an invalid observation page");
  }
}
export async function threadObservationEntry(
  request: ReadSessionEventsRequest,
  signal?: AbortSignal
): Promise<ReadSessionEventsResponse> {
  const requestedCursor = request.afterCursor + 1n;
  if (request.limit !== 1) throw new Error("Chronological observation entry requests must have limit 1");
  if (requestedCursor < 1n) {
    throw new HttpError(new Response(null, { status: 404 }), `no observation at ${requestedCursor} in this thread`);
  }

  const sessionId = encodeURIComponent(request.sessionId);
  const summaryRequest = create(GetSessionSummaryRequestSchema, { sessionId: request.sessionId });
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
      throw new Error(`History Service has not committed cursor ${requestedCursor} used by its fold`);
    }
    throw new HttpError(new Response(null, { status: 404 }), `no observation at ${requestedCursor} in this thread`);
  }

  const eventsUrl = new URL(`/sessions/${sessionId}/events`, window.location.href);
  eventsUrl.searchParams.set("after_cursor", request.afterCursor.toString());
  eventsUrl.searchParams.set("limit", String(request.limit));
  const page = fromJson(
    ReadSessionEventsResponseSchema,
    await historyJson(eventsUrl.pathname + eventsUrl.search, { signal })
  );
  const unstored = [...page.settlements.flatMap((settlement) => settlement.ranges), ...page.removed];
  if (nextStored(requestedCursor - 1n, unstored) !== requestedCursor) {
    throw new HttpError(new Response(null, { status: 404 }), `no observation at ${requestedCursor} in this thread`);
  }
  if (page.entries.length !== 1 || page.entries[0]!.cursor !== requestedCursor) {
    throw new Error(`History Service did not return requested cursor ${requestedCursor}`);
  }
  return page;
}

export async function threadObservations(
  request: ReadSessionObservationsRequest,
  signal?: AbortSignal
): Promise<ReadSessionObservationsResponse> {
  const url = new URL(`/sessions/${encodeURIComponent(request.sessionId)}/observations`, window.location.href);
  url.searchParams.set("limit", String(request.limit));
  if (request.beforeCursor !== undefined) url.searchParams.set("before_cursor", request.beforeCursor.toString());
  if (request.afterCursor !== undefined) url.searchParams.set("after_cursor", request.afterCursor.toString());
  const response = fromJson(
    ReadSessionObservationsResponseSchema,
    await historyJson(url.pathname + url.search, { signal })
  );
  validateObservationPage(response, request);
  return response;
}

export async function threadEvidence(
  request: ReadEntityEvidenceRequest,
  signal?: AbortSignal
): Promise<ReadEntityEvidenceResponse> {
  const response = fromJson(
    ReadEntityEvidenceResponseSchema,
    await historyJson(`/sessions/${encodeURIComponent(request.sessionId)}/entity-evidence`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: bodyWithoutSessionId(toJson(ReadEntityEvidenceRequestSchema, request)),
      signal,
    })
  );
  return response;
}

export async function threadNativeFrames(
  request: ReadNativeFramesRequest,
  signal?: AbortSignal
): Promise<ReadNativeFramesResponse> {
  const response = fromJson(
    ReadNativeFramesResponseSchema,
    await historyJson(`/sessions/${encodeURIComponent(request.sessionId)}/native-frames`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: bodyWithoutSessionId(toJson(ReadNativeFramesRequestSchema, request)),
      signal,
    })
  );
  return response;
}

/** An explicit Dispatcher rejection of this request, not a durable admission receipt. */
export class CommandSubmissionRefused extends Error {}

export type CommandSubmission = { kind: "dispatch"; dispatch: Dispatch };

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
  target: CommandTarget | null,
  commandId: string
): Dispatch {
  if (!dispatch) throw new Error("Command receipt could not be verified: missing Dispatch");
  if (
    dispatch.commandId !== commandId ||
    dispatch.sessionId !== threadId ||
    (target !== null && (dispatch.sandbox !== target.sandbox || dispatch.sandboxUid !== target.sandboxUid)) ||
    dispatch.state === DeliveryState.UNSPECIFIED
  )
    throw new Error("Command receipt could not be verified: mismatched Dispatcher destination or command");
  return dispatch;
}

export async function submitInput(
  threadId: string,
  target: CommandTarget | null,
  commandId: string,
  text: string
): Promise<CommandSubmission> {
  const request = create(SubmitInputRequestSchema, {
    sessionId: threadId,
    commandId,
    expectedSandboxUid: target?.sandboxUid,
    text,
  });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:submitInput`,
    bodyWithoutSessionId(toJson(SubmitInputRequestSchema, request))
  );
  const response = fromJson(SubmitInputResponseSchema, value);
  return { kind: "dispatch", dispatch: confirmedDispatch(response.dispatch, threadId, target, commandId) };
}

export async function changeModel(
  threadId: string,
  target: CommandTarget | null,
  commandId: string,
  model: string
): Promise<CommandSubmission> {
  const request = create(ChangeModelRequestSchema, {
    sessionId: threadId,
    commandId,
    expectedSandboxUid: target?.sandboxUid,
    model,
  });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:changeModel`,
    bodyWithoutSessionId(toJson(ChangeModelRequestSchema, request))
  );
  const response = fromJson(ChangeModelResponseSchema, value);
  return { kind: "dispatch", dispatch: confirmedDispatch(response.dispatch, threadId, target, commandId) };
}

export async function changeReasoningEffort(
  threadId: string,
  target: CommandTarget | null,
  commandId: string,
  effort: string
): Promise<CommandSubmission> {
  const request = create(ChangeReasoningEffortRequestSchema, {
    sessionId: threadId,
    commandId,
    expectedSandboxUid: target?.sandboxUid,
    effort,
  });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:changeReasoningEffort`,
    bodyWithoutSessionId(toJson(ChangeReasoningEffortRequestSchema, request))
  );
  const response = fromJson(ChangeReasoningEffortResponseSchema, value);
  return { kind: "dispatch", dispatch: confirmedDispatch(response.dispatch, threadId, target, commandId) };
}

export async function interruptTurn(
  threadId: string,
  target: CommandTarget | null,
  commandId: string,
  turnId: string
): Promise<CommandSubmission> {
  const request = create(InterruptTurnRequestSchema, {
    sessionId: threadId,
    commandId,
    expectedSandboxUid: target?.sandboxUid,
    turnId,
  });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:interruptTurn`,
    bodyWithoutSessionId(toJson(InterruptTurnRequestSchema, request))
  );
  const response = fromJson(InterruptTurnResponseSchema, value);
  return { kind: "dispatch", dispatch: confirmedDispatch(response.dispatch, threadId, target, commandId) };
}

/** Reads the current projection scope through History's generated HTTP facade. The Session may
 * not have been recorded yet, or its fold may not have materialized; both are resolved by the
 * caller's WatchSessionSummaries subscription. */
export async function threadScope(threadId: string, signal?: AbortSignal): Promise<ReadThreadEntitiesResponse | null> {
  const request = create(ReadThreadEntitiesRequestSchema, {
    sessionId: threadId,
    projectionEpoch: "",
    window: { case: "range", value: { startEntityIndex: 0n, limit: 1 } },
  });
  try {
    const value = await historyJson(`/sessions/${encodeURIComponent(threadId)}/entities`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: bodyWithoutSessionId(toJson(ReadThreadEntitiesRequestSchema, request)),
      signal,
    });
    return fromJson(ReadThreadEntitiesResponseSchema, value);
  } catch (error) {
    if (error instanceof HttpError && error.status === 404) return null;
    throw error;
  }
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
  validateThread(metadata, summary);
  return { metadata, summary };
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

function assertKnownEnum(
  value: number,
  schema: { values: readonly { number: number; name: string }[] },
  label: string
): void {
  if (!schema.values.some((member) => member.number === value)) {
    throw new Error(`History Service returned an unknown ${label}: ${value}`);
  }
}

function validateThread(metadata: ThreadMetadata, summary: SessionSummary): void {
  if (!metadata.createdAt) throw new Error("App returned Thread metadata without created_at");
  const attachment = summary.attached;
  const spec = attachment?.spec;
  if (spec?.placement?.kind.case === "environmentHosted") {
    const harness = spec.placement.kind.value.harness;
    assertKnownEnum(harness, HarnessSchema, "Harness");
    if (harness === Harness.UNSPECIFIED) throw new Error("History Service returned an unspecified Harness");
  } else if (attachment && attachment.historicalHarness !== Harness.UNSPECIFIED) {
    // Archived snapshots may have no reconstructible environment or harness provenance.
    assertKnownEnum(attachment.historicalHarness, HarnessSchema, "historical Harness");
  }
  assertKnownEnum(attachment?.harnessState ?? HarnessState.UNSPECIFIED, HarnessStateSchema, "HarnessState");
  if (attachment) {
    assertKnownEnum(attachment.setupState, SetupStateSchema, "SetupState");
    assertKnownEnum(attachment.launchOutcome, LaunchOutcomeSchema, "LaunchOutcome");
  }
  // An attached Session can precede the first runner feed observation. History represents
  // that valid state with FEED_STATUS_UNSPECIFIED; preserve the former validation boundary.
  if (attachment && summary.feedStatus !== FeedStatus.UNSPECIFIED) {
    assertKnownEnum(summary.feedStatus, FeedStatusSchema, "FeedStatus");
  }
  if (summary.lastTurnStatus !== undefined) assertKnownEnum(summary.lastTurnStatus, TurnStatusSchema, "TurnStatus");
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
    if (listing.sandboxUID) query.set("sandbox_uid", listing.sandboxUID);
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
      if (listing.sandboxUID && summary.sandbox?.uid !== listing.sandboxUID) continue;
      const matchesSearch =
        search === null ||
        (metadataRow.name?.toLowerCase().includes(search) ?? false) ||
        metadataRow.threadId.toLowerCase().startsWith(search) ||
        sandboxMatches.has(metadataRow.threadId);
      if (!matchesSearch) continue;
      if (!summary.sandbox?.name) throw new Error("History Session has no Sandbox identity");
      validateThread(metadataRow, summary);
      threads.push({ metadata: metadataRow, summary });
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
  if (openThreadId !== null && !visible.some((thread) => thread.metadata.threadId === openThreadId)) {
    try {
      const opened = await findThread(openThreadId, signal);
      if (opened) visible.push(opened);
    } catch (reason) {
      if (signal?.aborted) throw reason;
    }
  }
  return { threads: visible, more, archived_count: archivedCount };
}

/** Set or clear the operator-managed name. */
export async function renameThread(threadId: string, name: string | null): Promise<ThreadMetadata> {
  const request = create(RenameThreadRequestSchema, { threadId, name: name ?? undefined });
  const metadata = await fetchJson(
    new Request(new URL(`/upstream/app/v1/threads/${encodeURIComponent(threadId)}`, window.location.href), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(toJson(RenameThreadRequestSchema, request)),
    })
  );
  return fromJson(ThreadMetadataSchema, metadata);
}

export async function archiveThread(threadId: string, archived: boolean): Promise<void> {
  const request = create(ThreadMetadataTargetSchema, { threadId });
  await fetchText(
    new Request(
      new URL(
        `/upstream/app/v1/threads/${encodeURIComponent(threadId)}/${archived ? "archive" : "unarchive"}`,
        window.location.href
      ),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(toJson(ThreadMetadataTargetSchema, request)),
      }
    )
  );
}
