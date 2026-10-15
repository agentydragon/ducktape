import { fromJson, toJson, type JsonObject, type JsonValue } from "@bufbuild/protobuf";
import createClient from "openapi-fetch";

import type { components, paths } from "./api/schema";
import { redirectToLogin } from "./operator_login";
import { AttachedSchema, SessionSpecSchema, type Attached, type SessionSpec } from "../../proto/runner_pb";
import { CommandSchema, type Command } from "../../proto/command_pb";
import { EventEntrySchema, type EventEntry } from "../../proto/event_log_pb";

export const api: ReturnType<typeof createClient<paths>> = createClient<paths>({ baseUrl: "" });

// 401 → app login; the never-settling promise keeps the page loading while the browser navigates away.
function unlessLoggedOut(response: Response): Response | Promise<never> {
  if (response.status === 401 && redirectToLogin()) return new Promise<never>(() => {});
  return response;
}

api.use({ onResponse: ({ response }) => unlessLoggedOut(response) });

/** `fetch` under `api`'s 401 policy, for the app's own routes that do not go through `api`. */
export async function fetchWithLogin(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  return unlessLoggedOut(await fetch(input, init));
}

/** A non-2xx answer from the API: its status, and its body as openapi-fetch decoded it. */
export class HttpError extends Error {
  readonly status: number;

  constructor(
    response: Response,
    readonly body: unknown
  ) {
    super(`HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}: ${displayableError(body)}`);
    this.status = response.status;
  }
}

/** The data of an `api` answer, or its error thrown as an `HttpError`. */
export function ok<T>(answer: { data?: T; error?: unknown; response: Response }): T {
  if (answer.error !== undefined) throw new HttpError(answer.response, answer.error);
  return answer.data as T;
}

export type SandboxView = components["schemas"]["SandboxView"];
export type PodView = components["schemas"]["PodView"];
export type RawResourceStatus = Record<string, unknown> | null;
export type NewSandbox = components["schemas"]["NewSandbox"];
export type ThreadView = components["schemas"]["ThreadView"];
export type ThreadScope = components["schemas"]["ThreadScopeResponse"];
export type ThreadEntityView = components["schemas"]["ThreadEntityView"];
export type EvidencePage = components["schemas"]["EvidencePage"];
export type NativeFramePage = components["schemas"]["NativeFramePage"];
export type ObservationPage = components["schemas"]["ObservationPage"];
export type ArchivedObservationEntry = components["schemas"]["ArchivedObservationEntry"];
export type BindingView = components["schemas"]["BindingView"];
export type PolicyView = components["schemas"]["PolicyView"];
export type SandboxPresetView = components["schemas"]["SandboxPresetView"];
export type SessionDefaults = components["schemas"]["SessionDefaults"];
export type SessionOpening = {
  sessionId: string;
  sandbox: string;
  state: string;
  reason?: string;
  attached?: Attached;
};
export type Harness = components["schemas"]["Harness"];
export type ModelOption = components["schemas"]["ModelOption"];
export type ModelCatalog = components["schemas"]["ModelCatalog"];

/** An empty model list disables new-session selection, not the native harness. */
export function harnessOptions(catalog: ModelCatalog | null): { value: Harness; label: string; disabled: boolean }[] {
  const harnesses: { value: Harness; label: string }[] = [
    { value: "HARNESS_CLAUDE", label: "Claude" },
    { value: "HARNESS_CODEX", label: "Codex" },
  ];
  return harnesses.map(({ value, label }) => {
    const disabled = !catalog?.harnesses[value]?.length;
    return { value, label: disabled && catalog ? `${label} (no models offered)` : label, disabled };
  });
}

/** The models offered for one harness, resolved from the catalog's deduplicated `models`
 * list via its `harnesses` id references. */
export function modelsForHarness(catalog: ModelCatalog, harness: Harness): ModelOption[] {
  const byId = new Map(catalog.models.map((option) => [option.model, option]));
  return (catalog.harnesses[harness] ?? []).flatMap((id) => {
    const option = byId.get(id);
    return option ? [option] : [];
  });
}
export type ComponentStatus = components["schemas"]["ComponentStatus"];
export type Decision = components["schemas"]["Decision"];
export type Connection = components["schemas"]["Connection"];
export type CallerServiceAccount = components["schemas"]["ServiceAccountRef"];

export async function threadObservationEntry(
  threadId: string,
  cursor: string,
  signal?: AbortSignal
): Promise<ArchivedObservationEntry> {
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

/** `namespace/name`, as kubectl spells a ServiceAccount; the key a picker selects by. */
export function serviceAccountKey(account: CallerServiceAccount): string {
  return `${account.namespace}/${account.name}`;
}

export function isEligibleCaller(caller: CallerServiceAccount, accounts: CallerServiceAccount[]): boolean {
  return accounts.some((account) => serviceAccountKey(account) === serviceAccountKey(caller));
}
export type McpLinkageView = components["schemas"]["McpLinkageView"];
export type McpLinkageStartView = components["schemas"]["McpLinkageStartView"];

export type CallerGrantView = components["schemas"]["CallerGrantView"];
export type CallerGrantReader = (account: CallerServiceAccount, signal: AbortSignal) => Promise<CallerGrantView>;

export const readCallerGrants: CallerGrantReader = async (account, signal) =>
  ok(await api.GET("/caller-grants/{namespace}/{name}", { params: { path: account }, signal }));

export async function listConnections(): Promise<Connection[]> {
  return ok(await api.GET("/connections"));
}

export async function connectionServiceAccounts(): Promise<CallerServiceAccount[]> {
  return ok(await api.GET("/connection-service-accounts"));
}

export async function rebindConnection(connection: Connection, caller: CallerServiceAccount): Promise<Connection> {
  return ok(
    await api.POST("/connections/{connection_id}/rebind", {
      params: { path: { connection_id: connection.id } },
      body: { expected_version: connection.version, service_account: caller },
    })
  );
}

export async function unbindConnection(connection: Connection): Promise<Connection> {
  return ok(
    await api.POST("/connections/{connection_id}/unbind", {
      params: { path: { connection_id: connection.id } },
      body: { expected_version: connection.version },
    })
  );
}

export async function startMcpLinkage(serverId: string, scopes: string[]): Promise<McpLinkageStartView> {
  return ok(
    await api.POST("/mcp-servers/{server_id}/linkage/start", {
      params: { path: { server_id: serverId } },
      body: { scopes },
    })
  );
}

export async function disconnectMcpLinkage(serverId: string): Promise<McpLinkageView> {
  return ok(
    await api.POST("/mcp-servers/{server_id}/linkage/disconnect", { params: { path: { server_id: serverId } } })
  );
}

export function displayableError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null) return JSON.stringify(error);
  return String(error);
}

/**
 * Create the Session `sessionId`, a fresh UUID, names. On an ambiguous response, look for its
 * Thread (`findThread`); never re-run bootstrap implicitly.
 */
export async function openSession(
  sandbox: string,
  sessionId: string,
  spec: SessionSpec,
  setupScript?: string
): Promise<SessionOpening> {
  const data = ok(
    await api.POST("/sandboxes/{name}/sessions", {
      params: { path: { name: sandbox } },
      body: { session_id: sessionId, spec: toJson(SessionSpecSchema, spec) as JsonObject, setup_script: setupScript },
    })
  );
  return data as SessionOpening;
}

export async function getSessionOpening(sandbox: string, sessionId: string): Promise<SessionOpening> {
  return ok(
    await api.GET("/sandboxes/{name}/sessions/{session_id}/opening", {
      params: { path: { name: sandbox, session_id: sessionId } },
    })
  ) as SessionOpening;
}

export async function resumeThread(threadId: string): Promise<Attached> {
  const data = ok(await api.POST("/threads/{thread_id}/resume", { params: { path: { thread_id: threadId } } }));
  return fromJson(AttachedSchema, data as JsonValue);
}

export async function models(): Promise<ModelCatalog> {
  return ok(await api.GET("/models"));
}

/** An explicit 4xx command rejection: unlike a lost reply, this is not an admission receipt. */
export class CommandSubmissionRefused extends Error {}

export interface DispatchReceipt {
  commandId: string;
  sessionId: string;
  state: string;
  reason?: string;
  updatedAt?: string;
  admission: EventEntry | null;
}

export type CommandSubmission =
  { kind: "admission"; admission: EventEntry } | { kind: "dispatch"; dispatch: DispatchReceipt };

function dispatchReceipt(value: unknown): DispatchReceipt {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Session Dispatcher returned an invalid Dispatch");
  }
  const row = value as Record<string, unknown>;
  if (typeof row.commandId !== "string" || typeof row.sessionId !== "string" || typeof row.state !== "string") {
    throw new Error("Session Dispatcher returned a Dispatch without its command, Session or state");
  }
  if (row.reason !== undefined && typeof row.reason !== "string") {
    throw new Error("Session Dispatcher returned an invalid Dispatch reason");
  }
  if (row.updatedAt !== undefined && typeof row.updatedAt !== "string") {
    throw new Error("Session Dispatcher returned an invalid Dispatch update time");
  }
  const admission = row.admission === undefined ? null : fromJson(EventEntrySchema, row.admission as JsonValue);
  return {
    commandId: row.commandId,
    sessionId: row.sessionId,
    state: row.state,
    reason: row.reason as string | undefined,
    updatedAt: row.updatedAt as string | undefined,
    admission,
  };
}

/** The runner's exact durable CommandAdmitted receipt; the app archive may still be catching up.
 * Replaying the same immutable command id/payload is safe after a lost response.
 */
export async function command(threadId: string, message: Command): Promise<CommandSubmission> {
  let result;
  try {
    result = await api.POST("/threads/{thread_id}/commands", {
      params: { path: { thread_id: threadId } },
      body: toJson(CommandSchema, message) as JsonObject,
    });
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
    const value = data as JsonValue;
    if (typeof value === "object" && value !== null && "event" in value) {
      return { kind: "admission", admission: fromJson(EventEntrySchema, value) };
    }
    return { kind: "dispatch", dispatch: dispatchReceipt(data) };
  } catch (cause) {
    throw new Error(`Command receipt could not be verified: ${displayableError(cause)}`, { cause });
  }
}

/** Read a queued command's current durable state. A removed Sandbox has no retained dispatch row. */
export async function commandDispatch(threadId: string, commandId: string): Promise<DispatchReceipt> {
  const result = await api.GET("/threads/{thread_id}/commands/{command_id}/dispatch", {
    params: { path: { thread_id: threadId, command_id: commandId } },
  });
  if (result.error !== undefined && result.response.status === 404) {
    return {
      commandId,
      sessionId: threadId,
      state: "DELIVERY_STATE_UNKNOWN",
      reason: "sandbox_deleted",
      admission: null,
    };
  }
  return dispatchReceipt(ok(result));
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
