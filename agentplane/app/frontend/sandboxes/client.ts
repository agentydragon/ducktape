import { create, fromJson, toJson, type JsonObject } from "@bufbuild/protobuf";
import { ValueSchema } from "@bufbuild/protobuf/wkt";

import type { components } from "../schema";
import { api, ok, fetchWithLogin, HttpError } from "../client";
import {
  EnvironmentRuntimeStatusSchema,
  SessionSpecSchema,
  type EnvironmentRuntimeStatus,
  type SessionSpec,
} from "../../../proto/session_pb";
import {
  GetEnvironmentRuntimeStatusRequestSchema,
  OpenSessionRequestSchema,
  GetSessionOpeningRequestSchema,
} from "../../app_service_pb";
import { SessionOpeningSchema, type SessionOpening } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";

type OtherRuleView = {
  kind: "other";
  number: number;
  type: string;
  fields: Record<string, unknown>;
};
type PolicyRuleView = EgressRuleView | OtherRuleView;
export type PolicySetView = Omit<components["schemas"]["PolicySetView"], "rules"> & { rules: PolicyRuleView[] };
export type BindingView = Omit<components["schemas"]["BindingView"], "policy_sets"> & { policy_sets: PolicySetView[] };
export type EgressRuleView = {
  kind: "egress";
  number: number;
  hosts: string[];
  methods: string[] | null;
  paths: string[] | null;
  credential: components["schemas"]["CredentialView"] | null;
  missing_credential: string | null;
};
/** Views carried by the raw live egress stream and the sandbox stream's Action policy field. */
export type Decision = {
  at: string;
  method: string;
  host: string;
  port: number;
  outcome: "allow" | "deny";
  reason: string | null;
  binding: string | null;
  policy: string | null;
  rule: number | null;
  substituted: boolean;
  address: string | null;
};
/** Submit once; an uncertain response is reconciled by the caller-minted Session ID. */
export async function openSession(
  sandbox: string,
  sandboxUID: string,
  sessionId: string,
  spec: SessionSpec,
  setupScript?: string | null
): Promise<SessionOpening> {
  const request = create(OpenSessionRequestSchema, {
    name: sandbox,
    sessionId,
    sandboxUid: sandboxUID,
    spec: toJson(SessionSpecSchema, spec) as JsonObject,
    setupScript: setupScript === undefined ? undefined : fromJson(ValueSchema, setupScript),
  });
  const body = toJson(OpenSessionRequestSchema, request, { useProtoFieldName: true }) as JsonObject;
  // The path supplies name. Older Huma replicas reject extra JSON properties.
  delete body.name;
  return sessionOpeningJson(`/sandboxes/${encodeURIComponent(request.name)}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // Preserve the request spelling understood by app replicas and cached bundles during rollout.
    body: JSON.stringify(body),
  });
}

export async function getSessionOpening(sandbox: string, sessionId: string): Promise<SessionOpening> {
  const request = create(GetSessionOpeningRequestSchema, { name: sandbox, sessionId });
  return sessionOpeningJson(
    `/sandboxes/${encodeURIComponent(request.name)}/sessions/${encodeURIComponent(request.sessionId)}/opening`
  );
}

async function sessionOpeningJson(path: string, init?: RequestInit): Promise<SessionOpening> {
  const response = await fetchWithLogin(path, init);
  const body = await response.json();
  if (!response.ok) throw new HttpError(response, body);
  return fromJson(SessionOpeningSchema, body);
}

/** During rollout an older app replica can still return its Huma HTTP error. */
export function sessionOpeningRejected(error: unknown): boolean {
  if (!(error instanceof HttpError)) return false;
  if (error.code !== undefined) {
    // INVALID_ARGUMENT, NOT_FOUND, ALREADY_EXISTS, PERMISSION_DENIED, FAILED_PRECONDITION,
    // ABORTED and UNAUTHENTICATED describe a rejected submission. Other results remain uncertain.
    return [3, 5, 6, 7, 9, 10, 16].includes(error.code);
  }
  return error.status >= 400 && error.status < 500 && error.status !== 408;
}

export function sessionOpeningNotVisible(error: unknown): boolean {
  return (
    error instanceof HttpError &&
    (error.code !== undefined ? error.code === 5 || error.code === 14 : error.status === 404 || error.status === 503)
  );
}

export async function getEnvironmentRuntimeStatus(sandbox: string): Promise<EnvironmentRuntimeStatus> {
  const request = create(GetEnvironmentRuntimeStatusRequestSchema, { name: sandbox });
  const response = await fetchWithLogin(`/sandboxes/${encodeURIComponent(request.name)}/runner-status`, {
    headers: { Accept: "application/json" },
  });
  const text = await response.text();
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    body = text;
  }
  if (!response.ok) throw new HttpError(response, body);
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    throw new Error("Runner status response must be a JSON object");
  }
  return fromJson(EnvironmentRuntimeStatusSchema, body as JsonObject);
}

/** Asks for the Sandbox to run; the watch behind the live streams brings back the change. */
export async function resumeSandbox(name: string, uid: string): Promise<void> {
  ok(await api.POST("/sandboxes/{name}/resume", { params: { path: { name } }, body: { uid } }));
}
