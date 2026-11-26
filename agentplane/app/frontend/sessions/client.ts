import { create, fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";

import { sessionDispatcherClient } from "../transport/session_dispatcher_client";
import { displayableError, fetchJson, fetchText, fetchWithLogin, HttpError } from "../transport/client";
import { TurnStatusSchema } from "../../../proto/event_pb";
import { type ExactEnvironment, Harness, HarnessSchema } from "../../../proto/session_pb";
import {
  ListThreadMetadataResponseSchema,
  RenameThreadRequestSchema,
  ThreadMetadataTargetSchema,
  ThreadMetadataSchema,
  type ThreadMetadata,
} from "../../app_service_pb";
import {
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
} from "../../../history_service/api/history/history_service_pb";
import {
  BatchGetDispatchesResponseSchema,
  type BatchGetDispatchesResponse_Result,
  ChangeModelRequestSchema,
  ChangeModelResponseSchema,
  ChangeReasoningEffortRequestSchema,
  ChangeReasoningEffortResponseSchema,
  DeliveryState,
  DispatchSchema,
  GetDispatchResponseSchema,
  ListSessionDispatchesResponseSchema,
  ListSessionDispatchesResponse_Coverage,
  type ListSessionDispatchesResponse,
  InterruptTurnRequestSchema,
  InterruptTurnResponseSchema,
  SubmitInputRequestSchema,
  SubmitInputResponseSchema,
  type Dispatch,
  type Session,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { facadeBody } from "./facade_body";

/** App metadata and its History projection stay in their generated wire shapes. */
export interface ThreadView {
  metadata: ThreadMetadata;
  summary: SessionSummary;
  session?: Session;
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
      body: facadeBody(toJson(ReadEntityEvidenceRequestSchema, request), "sessionId"),
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
      body: facadeBody(toJson(ReadNativeFramesRequestSchema, request), "sessionId"),
      signal,
    })
  );
  return response;
}

/** An explicit Dispatcher rejection of this request, not a durable admission receipt. */
export class CommandSubmissionRefused extends Error {}

export type CommandSubmission = { kind: "dispatch"; dispatch: Dispatch };

const deliveryStateNumbers = new Set(
  Object.values(DeliveryState).filter((value) => typeof value === "number") as number[]
);
const deliveryStateNames = new Set(
  Object.keys(DeliveryState)
    .filter((value) => Number.isNaN(Number(value)))
    .map((value) => `DELIVERY_STATE_${value}`)
);

/** Map a value from a newer or older Dispatcher binary to the named unknown variant. */
export function normalizeDeliveryState(state: DeliveryState): DeliveryState {
  if (state === DeliveryState.UNSPECIFIED || !deliveryStateNumbers.has(state)) return DeliveryState.UNKNOWN;
  return state;
}

function normalizeDispatchJson(value: unknown): JsonValue {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return value as JsonValue;
  const row = value as Record<string, unknown>;
  const state = row.state;
  if (typeof state === "string") {
    const normalizedState =
      deliveryStateNames.has(state) && state !== "DELIVERY_STATE_UNSPECIFIED" ? state : "DELIVERY_STATE_UNKNOWN";
    return { ...row, state: normalizedState } as JsonValue;
  }
  if (typeof state === "number") {
    return { ...row, state: normalizeDeliveryState(state as DeliveryState) } as JsonValue;
  }
  return value as JsonValue;
}

function normalizeDispatchResponseJson(value: JsonValue): JsonValue {
  if (typeof value !== "object" || value === null || Array.isArray(value) || value.dispatch == null) return value;
  return { ...value, dispatch: normalizeDispatchJson(value.dispatch) };
}

function decodeDispatchFromJson(value: unknown, requireDestination: boolean): Dispatch {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Session Dispatcher returned an invalid Dispatch");
  }
  const normalized = normalizeDispatchJson(value);
  const row = normalized as Record<string, unknown>;
  if (
    (requireDestination && (typeof row.commandId !== "string" || typeof row.sessionId !== "string")) ||
    (typeof row.state !== "string" && typeof row.state !== "number")
  ) {
    throw new Error("Session Dispatcher returned a Dispatch without its command, Session or state");
  }
  const dispatch = fromJson(DispatchSchema, normalized);
  return { ...dispatch, state: normalizeDeliveryState(dispatch.state) };
}

export function dispatchFromJson(value: unknown): Dispatch {
  return decodeDispatchFromJson(value, true);
}

/** Older local interrupt receipts omitted the command and Session fields. */
export function savedInterruptResultFromJson(value: unknown): Dispatch {
  return decodeDispatchFromJson(value, false);
}

/** A timeout, lost response, stale destination, or missing Sandbox does not prove an earlier
 * attempt was never admitted. Only invariant/auth rejections are known refusals. */
async function dispatcherCommand(path: string, body: string): Promise<JsonValue> {
  let response: Response;
  try {
    response = await sessionDispatcherClient.post(path, body);
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

function confirmedDispatch(dispatch: Dispatch | undefined, threadId: string, commandId: string): Dispatch {
  if (!dispatch) throw new Error("Command receipt could not be verified: missing Dispatch");
  const state = normalizeDeliveryState(dispatch.state);
  if (
    dispatch.commandId !== commandId ||
    dispatch.sessionId !== threadId ||
    (dispatch.sandbox === "") !== (dispatch.sandboxUid === "") ||
    dispatch.state === DeliveryState.UNSPECIFIED
  )
    throw new Error("Command receipt could not be verified: mismatched Dispatcher destination or command");
  return { ...dispatch, state };
}

export async function listSessionDispatches(
  sessionId: string,
  { pageToken = "", limit = 25, signal }: { pageToken?: string; limit?: number; signal?: AbortSignal } = {}
): Promise<ListSessionDispatchesResponse> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (pageToken) query.set("pageToken", pageToken);
  const json = await fetchJson(
    `/upstream/dispatcher/v1/sessions/${encodeURIComponent(sessionId)}/dispatches?${query}`,
    {
      signal,
      headers: { Accept: "application/json" },
    }
  );
  const page = fromJson(ListSessionDispatchesResponseSchema, json, { ignoreUnknownFields: true });
  const ids = new Set(page.dispatches.map((row) => row.commandId));
  if (
    page.coverage !== ListSessionDispatchesResponse_Coverage.RETAINED_DISPATCH_ROWS ||
    page.dispatches.length > limit ||
    ids.size !== page.dispatches.length ||
    page.dispatches.some((row) => row.sessionId !== sessionId || !row.commandId)
  )
    throw new Error("Dispatcher returned an invalid receipt page");
  return page;
}

export const MAX_DISPATCH_BATCH = 16;

/** Read complete, ordered results for selected receipts in one Session. */
export async function batchGetDispatches(
  sessionId: string,
  ids: readonly string[],
  signal: AbortSignal
): Promise<BatchGetDispatchesResponse_Result[]> {
  if (!ids.length || ids.length > MAX_DISPATCH_BATCH || ids.includes("") || new Set(ids).size !== ids.length)
    throw new Error("Invalid receipt IDs");
  const query = new URLSearchParams();
  for (const id of ids) query.append("commandIds", id);
  let value: JsonValue;
  try {
    value = await fetchJson(
      `/upstream/dispatcher/v1/sessions/${encodeURIComponent(sessionId)}/dispatches:batchGet?${query}`,
      { signal, cache: "no-store", headers: { Accept: "application/json" } }
    );
  } catch (error) {
    // Large originals can exceed the aggregate transport limit. Split the read without changing its order.
    if (ids.length < 2 || !(error instanceof HttpError) || error.code !== 8) throw error;
    const middle = Math.ceil(ids.length / 2);
    const first = await batchGetDispatches(sessionId, ids.slice(0, middle), signal);
    return [...first, ...(await batchGetDispatches(sessionId, ids.slice(middle), signal))];
  }
  const { results } = fromJson(BatchGetDispatchesResponseSchema, value, { ignoreUnknownFields: true });
  if (results.length !== ids.length) throw new Error("Incomplete Dispatcher batch");
  for (const [index, row] of results.entries()) {
    if (row.commandId !== ids[index]) throw new Error("Unexpected command in Dispatcher batch");
    if (row.result.case === "notFound") continue;
    if (row.result.case !== "dispatch" && row.result.case !== "legacyDeletionReceipt")
      throw new Error("Missing Dispatcher result");
    const receipt = confirmedDispatch(row.result.value, sessionId, row.commandId);
    if ((receipt.receiptVersion ?? 0n) <= 0n) throw new Error("Dispatcher omitted a receipt revision");
    if (row.result.case === "legacyDeletionReceipt" && (receipt.sandbox || receipt.sandboxUid || receipt.command))
      throw new Error("Invalid legacy deletion receipt");
    row.result.value = receipt;
  }
  return results;
}

/** A missing current receipt leaves any earlier submission unconfirmed. */
export function getDispatch(threadId: string, commandId: string, signal?: AbortSignal): Promise<Dispatch | null> {
  return fetchProto(
    `/upstream/dispatcher/v1/dispatches/${encodeURIComponent(commandId)}`,
    (value) =>
      confirmedDispatch(
        fromJson(GetDispatchResponseSchema, normalizeDispatchResponseJson(value)).dispatch,
        threadId,
        commandId
      ),
    "Dispatcher receipt",
    signal
  );
}

export async function submitInput(threadId: string, commandId: string, text: string): Promise<CommandSubmission> {
  const request = create(SubmitInputRequestSchema, {
    sessionId: threadId,
    commandId,
    text,
  });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:submitInput`,
    facadeBody(toJson(SubmitInputRequestSchema, request), "sessionId")
  );
  const response = fromJson(SubmitInputResponseSchema, normalizeDispatchResponseJson(value));
  return { kind: "dispatch", dispatch: confirmedDispatch(response.dispatch, threadId, commandId) };
}

export async function changeModel(threadId: string, commandId: string, model: string): Promise<CommandSubmission> {
  const request = create(ChangeModelRequestSchema, {
    sessionId: threadId,
    commandId,
    model,
  });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:changeModel`,
    facadeBody(toJson(ChangeModelRequestSchema, request), "sessionId")
  );
  const response = fromJson(ChangeModelResponseSchema, normalizeDispatchResponseJson(value));
  return { kind: "dispatch", dispatch: confirmedDispatch(response.dispatch, threadId, commandId) };
}

export async function changeReasoningEffort(
  threadId: string,
  commandId: string,
  effort: string
): Promise<CommandSubmission> {
  const request = create(ChangeReasoningEffortRequestSchema, {
    sessionId: threadId,
    commandId,
    effort,
  });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:changeReasoningEffort`,
    facadeBody(toJson(ChangeReasoningEffortRequestSchema, request), "sessionId")
  );
  const response = fromJson(ChangeReasoningEffortResponseSchema, normalizeDispatchResponseJson(value));
  return { kind: "dispatch", dispatch: confirmedDispatch(response.dispatch, threadId, commandId) };
}

export async function interruptTurn(threadId: string, commandId: string, turnId: string): Promise<CommandSubmission> {
  const request = create(InterruptTurnRequestSchema, {
    sessionId: threadId,
    commandId,
    turnId,
  });
  const value = await dispatcherCommand(
    `/sessions/${encodeURIComponent(threadId)}:interruptTurn`,
    facadeBody(toJson(InterruptTurnRequestSchema, request), "sessionId")
  );
  const response = fromJson(InterruptTurnResponseSchema, normalizeDispatchResponseJson(value));
  return { kind: "dispatch", dispatch: confirmedDispatch(response.dispatch, threadId, commandId) };
}

/** Reads the current projection scope through History's generated HTTP facade. The Session may
 * not have been recorded yet, or its fold may not have materialized; both are resolved by the
 * caller's WatchSessionSummaries subscription. */
export async function threadScope(
  threadId: string,
  signal?: AbortSignal
): Promise<(ReadThreadEntitiesResponse & { cacheAccount: string | null }) | null> {
  const request = create(ReadThreadEntitiesRequestSchema, {
    sessionId: threadId,
    projectionEpoch: "",
    window: { case: "range", value: { startEntityIndex: 0n, limit: 1 } },
  });
  try {
    const response = await fetchWithLogin(
      new URL(`/upstream/history/v1/sessions/${encodeURIComponent(threadId)}/entities`, window.location.href),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: facadeBody(toJson(ReadThreadEntitiesRequestSchema, request), "sessionId"),
        signal,
      }
    );
    if (!response.ok) throw await HttpError.fromResponse(response);
    return Object.assign(fromJson(ReadThreadEntitiesResponseSchema, await response.json()), {
      cacheAccount: response.headers.get("Agentplane-Cache-Account"),
    });
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
  if (summary.lastTurnStatus !== undefined) assertKnownEnum(summary.lastTurnStatus, TurnStatusSchema, "TurnStatus");
}

/** Read all App-owned metadata pages. */
export async function listThreadMetadata(signal?: AbortSignal): Promise<Map<string, ThreadMetadata>> {
  const metadata = new Map<string, ThreadMetadata>();
  const tokens = new Set<string>();
  let token = "";
  for (;;) {
    const query = new URLSearchParams({ include_archived: "true", limit: "200" });
    if (token) query.set("page_token", token);
    const page = await fetchProto(
      `/upstream/app/v1/threads?${query}`,
      (json) => fromJson(ListThreadMetadataResponseSchema, json),
      "Thread metadata list",
      signal
    );
    if (!page) throw new Error("Thread metadata list was not found");
    for (const row of page.threads) metadata.set(row.threadId, row);
    if (!page.nextPageToken) break;
    if (tokens.has(page.nextPageToken)) throw new Error("Thread metadata page token did not advance");
    token = page.nextPageToken;
    tokens.add(token);
  }
  return metadata;
}

export function selectThreadWindow(
  listing: ThreadListing,
  openThreadId: string | null,
  sessions: readonly Session[],
  metadata: ReadonlyMap<string, ThreadMetadata>,
  summaries: ReadonlyMap<string, SessionSummary>
): ThreadWindow {
  const search = listing.search?.toLowerCase() ?? null;
  const candidates = sessions
    .filter((session) => {
      const environment = sessionEnvironment(session);
      return (
        (!listing.sandboxUID || environment?.uid === listing.sandboxUID) &&
        (search === null ||
          session.sessionId.toLowerCase().startsWith(search) ||
          metadata.get(session.sessionId)?.name?.toLowerCase().includes(search) ||
          environment?.name.toLowerCase().includes(search))
      );
    })
    .sort((left, right) => {
      const l = left.createdAt,
        r = right.createdAt;
      if (l?.seconds !== r?.seconds) return (l?.seconds ?? 0n) > (r?.seconds ?? 0n) ? -1 : 1;
      return (r?.nanos ?? 0) - (l?.nanos ?? 0) || right.sessionId.localeCompare(left.sessionId);
    });
  const archivedCount = candidates.filter((session) => metadata.get(session.sessionId)?.archived).length;
  const filtered = candidates.filter(
    (session) => listing.includeArchived || !metadata.get(session.sessionId)?.archived
  );
  const visible = filtered.slice(0, listing.limit);
  const opened = sessions.find((session) => session.sessionId === openThreadId);
  if (opened && !visible.some((session) => session.sessionId === opened.sessionId)) visible.push(opened);
  const threads = visible.map((session) => {
    const row =
      metadata.get(session.sessionId) ??
      create(ThreadMetadataSchema, { threadId: session.sessionId, createdAt: session.createdAt });
    const summary = summaries.get(session.sessionId) ?? create(SessionSummarySchema, { sessionId: session.sessionId });
    validateThread(row, summary);
    return { metadata: row, summary, session };
  });
  return { threads, more: filtered.length > listing.limit, archived_count: archivedCount };
}

export function sessionEnvironment(session: Session | undefined): ExactEnvironment | undefined {
  const placement = session?.placement?.kind;
  switch (placement?.case) {
    case "environmentHosted":
      return placement.value.environment;
    case "centralSamplerV1":
      return placement.value.executorEnvironment;
    default:
      return undefined;
  }
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
