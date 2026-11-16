import { create, fromJson, toJson, type JsonObject } from "@bufbuild/protobuf";
import { ValueSchema } from "@bufbuild/protobuf/wkt";

import { fetchJson, HttpError } from "../transport/client";
import type { EnvironmentTarget } from "../../../proto/identity_pb";
import { SessionSpecSchema, type SessionSpec } from "../../../proto/session_pb";
import { CreateSessionRequestSchema } from "../../app_service_pb";
import {
  GetSessionRequestSchema,
  GetSessionResponseSchema,
  ListSessionsResponseSchema,
  SessionSchema,
  type Session,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";

/** Submit once; an uncertain response is reconciled by the caller-minted Session ID. */
export async function createSession(
  target: EnvironmentTarget,
  sessionId: string,
  spec: SessionSpec,
  setupScript?: string | null
): Promise<Session> {
  const request = create(CreateSessionRequestSchema, {
    name: target.name,
    sessionId,
    sandboxUid: target.uid,
    spec: toJson(SessionSpecSchema, spec) as JsonObject,
    setupScript: setupScript === undefined ? undefined : fromJson(ValueSchema, setupScript),
  });
  const body = toJson(CreateSessionRequestSchema, request, { useProtoFieldName: true }) as JsonObject;
  // The path supplies the name; the app request body carries the remaining fields.
  delete body.name;
  const session = await sessionJson(`/sandboxes/${encodeURIComponent(request.name)}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (session.sessionId !== request.sessionId) {
    throw new Error(`App returned Session ${session.sessionId} for requested Session ${request.sessionId}`);
  }
  const placement = session.placement?.kind;
  const exactTarget =
    placement?.case === "environmentHosted"
      ? placement.value.environment
      : placement?.case === "centralSamplerV1"
        ? placement.value.executorEnvironment
        : undefined;
  if (exactTarget?.name !== target.name || exactTarget?.uid !== target.uid) {
    throw new Error("App acknowledged a different Environment identity");
  }
  return session;
}

async function sessionJson(path: string, init?: RequestInit): Promise<Session> {
  return fromJson(SessionSchema, await fetchJson(path, init));
}

export async function getSession(sessionId: string): Promise<Session> {
  const request = create(GetSessionRequestSchema, { sessionId });
  const body = await fetchJson(`/upstream/dispatcher/v1/sessions/${encodeURIComponent(request.sessionId)}`);
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    throw new Error("Dispatcher Session response must be a JSON object");
  }
  const session = fromJson(GetSessionResponseSchema, body).session;
  if (!session) throw new Error("Dispatcher returned no Session");
  if (session.sessionId !== request.sessionId) {
    throw new Error(`Dispatcher returned Session ${session.sessionId} for requested Session ${request.sessionId}`);
  }
  return session;
}

/** Classify explicit create rejections; transport failures remain uncertain. */
export function sessionCreateRejected(error: unknown): boolean {
  if (!(error instanceof HttpError)) return false;
  if (error.code !== undefined) {
    // INVALID_ARGUMENT, NOT_FOUND, ALREADY_EXISTS, PERMISSION_DENIED, FAILED_PRECONDITION,
    // ABORTED and UNAUTHENTICATED describe a rejected submission. Other results remain uncertain.
    return [3, 5, 6, 7, 9, 10, 16].includes(error.code);
  }
  return error.status >= 400 && error.status < 500 && error.status !== 408;
}

export function sessionNotVisible(error: unknown): boolean {
  return (
    error instanceof HttpError &&
    (error.code !== undefined ? error.code === 5 || error.code === 14 : error.status === 404 || error.status === 503)
  );
}

/** Read every Dispatcher page before claiming that no other Session names an Identity. */
export async function listIdentitySessions(): Promise<Session[]> {
  const sessions: Session[] = [];
  const visited = new Set<string>();
  let token = "";
  do {
    const query = new URLSearchParams({ limit: "100" });
    if (token) query.set("pageToken", token);
    const response = fromJson(
      ListSessionsResponseSchema,
      await fetchJson(`/upstream/dispatcher/v1/sessions?${query}`, { headers: { Accept: "application/json" } })
    );
    sessions.push(...response.sessions);
    token = response.nextPageToken;
    if (token && visited.has(token)) throw new Error("Dispatcher repeated a Session page token");
    visited.add(token);
  } while (token);
  return sessions;
}
