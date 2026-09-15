import { fromJson, toJson, type JsonObject, type JsonValue } from "@bufbuild/protobuf";
import createClient from "openapi-fetch";

import type { components, paths } from "./api/schema";
import { redirectToLogin } from "./operator_login";
import {
  AttachedSchema,
  SessionSpecSchema,
  SessionSummarySchema,
  type Attached,
  type SessionSpec,
  type SessionSummary,
} from "../../runner/protocol_pb";
import { CommandSchema, type Command } from "../../protocol/command_pb";
import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";

export const api: ReturnType<typeof createClient<paths>> = createClient<paths>({ baseUrl: "" });

// A 401 means the session expired or never existed; the app owns its login, so the browser goes
// there rather than the page rendering an error it cannot act on. Inert where something in front
// of the app does the authenticating, since then no request of ours is ever answered 401. While
// the login navigation is in flight the document is being replaced, so the request never settles:
// the page keeps its loading state instead of flashing the 401 it cannot act on.
api.use({
  onResponse({ response }) {
    if (response.status === 401 && redirectToLogin()) return new Promise<never>(() => {});
    return response;
  },
});

export type SandboxView = components["schemas"]["SandboxView"];
export type NewSandbox = components["schemas"]["NewSandbox"];
export type Condition = components["schemas"]["Condition"];
export type ThreadView = components["schemas"]["ThreadView"];
export type BindingView = components["schemas"]["BindingView"];
export type PolicyView = components["schemas"]["PolicyView"];
export type ActionPolicyView = components["schemas"]["ActionPolicyView"];
export type ActionPolicyUnavailable = components["schemas"]["ActionPolicyUnavailable"];
export type ActionPolicyBindingView = components["schemas"]["ActionPolicyBindingView"];
export type ActionPolicySetView = components["schemas"]["ActionPolicySetView"];
export type EffectivePolicyView = components["schemas"]["EffectivePolicyView"];
export type ReadyConditionView = components["schemas"]["ReadyConditionView"];
export type SandboxPresetView = components["schemas"]["SandboxPresetView"];
export type ThreadDefaults = components["schemas"]["ThreadDefaults"];
export type Harness = components["schemas"]["Harness"];
export type ModelCatalog = Record<Harness, string[]>;
export type Decision = components["schemas"]["Decision"];
export type ActionRequestView = components["schemas"]["ActionRequestView"];
export type ActionState = components["schemas"]["ActionState"];
export type Verdict = components["schemas"]["Verdict"];
export type Connection = components["schemas"]["Connection"];
export type CallerServiceAccount = components["schemas"]["ServiceAccountRef"];

/** `namespace/name`, as kubectl spells a ServiceAccount; the key a picker selects by. */
export function serviceAccountKey(account: CallerServiceAccount): string {
  return `${account.namespace}/${account.name}`;
}

export function isEligibleCaller(caller: CallerServiceAccount, accounts: CallerServiceAccount[]): boolean {
  return accounts.some((account) => serviceAccountKey(account) === serviceAccountKey(caller));
}
export type McpLinkageView = components["schemas"]["McpLinkageView"];
export type McpLinkageStartView = components["schemas"]["McpLinkageStartView"];
export class ConnectionRequestError extends Error {
  constructor(
    public readonly status: number,
    message: string
  ) {
    super(message);
  }
}

export interface ConnectionService {
  list(): Promise<Connection[]>;
  callerServiceAccounts(): Promise<CallerServiceAccount[]>;
  rename(connection: Connection, displayName: string): Promise<Connection>;
  unbind(connection: Connection): Promise<Connection>;
}

export const connectionService: ConnectionService = {
  async list() {
    const { data, error, response } = await api.GET("/connections");
    const status = response.status;
    if (error) throw new ConnectionRequestError(status, displayableError(error));
    return data;
  },
  async callerServiceAccounts() {
    const { data, error, response } = await api.GET("/connection-service-accounts");
    const status = response.status;
    if (error) throw new ConnectionRequestError(status, displayableError(error));
    return data;
  },
  async rename(connection, displayName) {
    const { data, error, response } = await api.PATCH("/connections/{connection_id}", {
      params: { path: { connection_id: connection.id } },
      body: { display_name: displayName, expected_version: connection.version },
    });
    if (error) throw new ConnectionRequestError(response.status, displayableError(error));
    return data;
  },
  async unbind(connection) {
    const { data, error, response } = await api.POST("/connections/{connection_id}/unbind", {
      params: { path: { connection_id: connection.id } },
      body: { expected_version: connection.version },
    });
    if (error) throw new ConnectionRequestError(response.status, displayableError(error));
    return data;
  },
};

export interface McpLinkageService {
  list(): Promise<McpLinkageView[]>;
  status(serverId: string): Promise<McpLinkageView>;
  start(serverId: string, scopes: string[]): Promise<McpLinkageStartView>;
  disconnect(serverId: string): Promise<McpLinkageView>;
}

export const mcpLinkageService: McpLinkageService = {
  async list() {
    const { data, error, response } = await api.GET("/mcp-servers");
    if (error) throw new Error(httpError(response, error));
    return data;
  },
  async status(serverId) {
    const { data, error, response } = await api.GET("/mcp-servers/{server_id}/linkage", {
      params: { path: { server_id: serverId } },
    });
    if (error) throw new Error(httpError(response, error));
    return data;
  },
  async start(serverId, scopes) {
    const { data, error, response } = await api.POST("/mcp-servers/{server_id}/linkage/start", {
      params: { path: { server_id: serverId } },
      body: { scopes },
    });
    if (error) throw new Error(httpError(response, error));
    return data;
  },
  async disconnect(serverId) {
    const { data, error, response } = await api.POST("/mcp-servers/{server_id}/linkage/disconnect", {
      params: { path: { server_id: serverId } },
    });
    if (error) throw new Error(httpError(response, error));
    return data;
  },
};

export interface ActionService {
  list(): Promise<ActionRequestView[]>;
  decide(request: ActionRequestView, verdict: Verdict): Promise<ActionRequestView>;
}

export const actionService: ActionService = {
  async list(): Promise<ActionRequestView[]> {
    const { data, error, response } = await api.GET("/actions");
    if (error) throw new Error(httpError(response, error));
    return data;
  },

  async decide(request: ActionRequestView, verdict: Verdict): Promise<ActionRequestView> {
    const { data, error, response } = await api.POST("/actions/{request_id}/decision", {
      params: { path: { request_id: request.id } },
      body: { verdict, expected_version: request.version, idempotency_key: crypto.randomUUID(), decision_note: null },
    });
    if (error) throw new Error(httpError(response, error));
    return data;
  },
};

export function httpError(response: Response, error: unknown): string {
  return `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}: ${displayableError(error)}`;
}

export function displayableError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null) return JSON.stringify(error);
  return String(error);
}

/**
 * The bridge's session routes carry proto-JSON of the runner protocol's messages, typed here by
 * protobuf-es from the runner and shared protocol files themselves; the OpenAPI document knows
 * them only as objects.
 */
export class RunnerUnavailableError extends Error {}

export async function listSessions(sandbox: string): Promise<SessionSummary[]> {
  const { data, error, response } = await api.GET("/sandboxes/{name}/sessions", {
    params: { path: { name: sandbox } },
  });
  // These routes report a missing Pod address as 409 and an unavailable runner as 503.
  if (error && (response.status === 409 || response.status === 503)) {
    throw new RunnerUnavailableError(displayableError(error));
  }
  if (error) throw new Error(displayableError(error));
  return data.map((row) => fromJson(SessionSummarySchema, row as JsonValue));
}

export async function openSession(sandbox: string, sessionId: string, spec: SessionSpec): Promise<Attached> {
  const { data, error } = await api.POST("/sandboxes/{name}/sessions", {
    params: { path: { name: sandbox } },
    body: { session_id: sessionId, spec: toJson(SessionSpecSchema, spec) as JsonObject },
  });
  if (error) throw new Error(displayableError(error));
  return fromJson(AttachedSchema, data as JsonValue);
}

export async function models(): Promise<ModelCatalog> {
  const { data, error } = await api.GET("/models");
  if (error) throw new Error(displayableError(error));
  return data as ModelCatalog;
}

/**
 * The saved command boundary: this is the exact archived CommandAdmitted EventEntry, not a
 * prediction that a harness has already executed the operation. Replaying the same immutable
 * command returns that same entry, so a lost HTTP response is safe to retry.
 */
export async function command(threadId: string, message: Command): Promise<EventEntry> {
  const { data, error } = await api.POST("/threads/{thread_id}/commands", {
    params: { path: { thread_id: threadId } },
    body: toJson(CommandSchema, message) as JsonObject,
  });
  if (error) throw new Error(displayableError(error));
  return fromJson(EventEntrySchema, data as JsonValue);
}

export function eventsUrl(threadId: string): string {
  return `/threads/${encodeURIComponent(threadId)}/events/stream`;
}

export async function getThread(threadId: string): Promise<ThreadView> {
  const { data, error } = await api.GET("/threads/{thread_id}", { params: { path: { thread_id: threadId } } });
  if (error) throw new Error(displayableError(error));
  return data;
}

/** A session's thread, or null before the bridge has opened the session. */
export async function findThread(sandbox: string, sessionId: string): Promise<ThreadView | null> {
  const { data, error } = await api.GET("/threads", {
    params: { query: { sandbox, session_id: sessionId, include_archived: true } },
  });
  if (error) throw new Error(displayableError(error));
  return data[0] ?? null;
}

/** Set the thread's name; null (or a blank, which the API normalises) leaves it unnamed. */
export async function renameThread(threadId: string, name: string | null): Promise<ThreadView> {
  const { data, error } = await api.PATCH("/threads/{thread_id}", {
    params: { path: { thread_id: threadId } },
    body: { name },
  });
  if (error) throw new Error(displayableError(error));
  return data;
}

export async function archiveThread(threadId: string, archived: boolean): Promise<void> {
  const { error } = await api.POST(`/threads/{thread_id}/${archived ? "archive" : "unarchive"}`, {
    params: { path: { thread_id: threadId } },
  });
  if (error) throw new Error(displayableError(error));
}
