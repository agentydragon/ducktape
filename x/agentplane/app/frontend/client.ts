import { create, fromJson, toJson, type JsonObject, type JsonValue } from "@bufbuild/protobuf";
import createClient from "openapi-fetch";

import type { components, paths } from "./api/schema";
import {
  AttachedSchema,
  InputSchema,
  SessionSpecSchema,
  SessionSummarySchema,
  type Attached,
  type SessionSpec,
  type SessionSummary,
} from "./protocol_pb";

export const api: ReturnType<typeof createClient<paths>> = createClient<paths>({ baseUrl: "" });

export type SandboxView = components["schemas"]["SandboxView"];
export type NewSandbox = components["schemas"]["NewSandbox"];
export type Condition = components["schemas"]["Condition"];

export function displayableError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null && "detail" in error) return String(error.detail);
  return String(error);
}

/**
 * The bridge's session routes carry proto-JSON of the runner protocol's messages, typed here by
 * protobuf-es from protocol.proto itself; the OpenAPI document knows them only as objects.
 */
export async function listSessions(sandbox: string): Promise<SessionSummary[]> {
  const { data, error } = await api.GET("/sandboxes/{name}/sessions", { params: { path: { name: sandbox } } });
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

export async function sendInput(sandbox: string, sessionId: string, inputId: string, text: string): Promise<void> {
  const { error } = await api.POST("/sandboxes/{name}/sessions/{session_id}/inputs", {
    params: { path: { name: sandbox, session_id: sessionId } },
    body: toJson(InputSchema, create(InputSchema, { inputId, text })) as JsonObject,
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
