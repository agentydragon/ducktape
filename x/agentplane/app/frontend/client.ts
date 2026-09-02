import createClient from "openapi-fetch";

import type { components, paths } from "./api/schema";

export const api: ReturnType<typeof createClient<paths>> = createClient<paths>({ baseUrl: "" });

export type SandboxView = components["schemas"]["SandboxView"];
export type NewSandbox = components["schemas"]["NewSandbox"];
export type Condition = components["schemas"]["Condition"];
// The runner protocol's messages, published from protocol.proto under a Runner prefix.
export type Event = components["schemas"]["RunnerEvent"];
export type Attached = components["schemas"]["RunnerAttached"];
export type SessionSummary = components["schemas"]["RunnerSessionSummary"];
export type SessionSpec = components["schemas"]["RunnerSessionSpec"];

export function displayableError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null && "detail" in error) return String(error.detail);
  return String(error);
}

/** The bridge's session routes carry proto-JSON, which the schema types as the protocol's messages. */
export async function listSessions(sandbox: string): Promise<SessionSummary[]> {
  const { data, error } = await api.GET("/sandboxes/{name}/sessions", { params: { path: { name: sandbox } } });
  if (error) throw new Error(displayableError(error));
  return data;
}

export async function openSession(sandbox: string, sessionId: string, spec: SessionSpec): Promise<Attached> {
  const { data, error } = await api.POST("/sandboxes/{name}/sessions", {
    params: { path: { name: sandbox } },
    body: { session_id: sessionId, spec },
  });
  if (error) throw new Error(displayableError(error));
  return data;
}

export async function sendInput(sandbox: string, sessionId: string, inputId: string, text: string): Promise<void> {
  const { error } = await api.POST("/sandboxes/{name}/sessions/{session_id}/inputs", {
    params: { path: { name: sandbox, session_id: sessionId } },
    body: { inputId, text },
  });
  if (error) throw new Error(displayableError(error));
}

export async function interruptSession(sandbox: string, sessionId: string): Promise<void> {
  const { error } = await api.POST("/sandboxes/{name}/sessions/{session_id}/interrupt", {
    params: { path: { name: sandbox, session_id: sessionId } },
  });
  if (error) throw new Error(displayableError(error));
}

export async function shutdownSession(sandbox: string, sessionId: string): Promise<void> {
  const { error } = await api.POST("/sandboxes/{name}/sessions/{session_id}/shutdown", {
    params: { path: { name: sandbox, session_id: sessionId } },
  });
  if (error) throw new Error(displayableError(error));
}

export function eventsUrl(sandbox: string, sessionId: string): string {
  return `/sandboxes/${encodeURIComponent(sandbox)}/sessions/${encodeURIComponent(sessionId)}/events`;
}
