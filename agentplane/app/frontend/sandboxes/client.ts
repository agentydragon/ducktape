import { create, fromJson, toJson, type JsonObject } from "@bufbuild/protobuf";
import { DurationSchema, ValueSchema } from "@bufbuild/protobuf/wkt";

import type { components } from "../schema";
import { fetchWithLogin, HttpError } from "../client";
import { EnvironmentTargetSchema, type EnvironmentTarget } from "../../../proto/identity_pb";
import {
  CreateEnvironmentRequestSchema,
  type CreateEnvironmentRequest,
  EnvironmentMutationRequestSchema,
  ListEnvironmentTemplatesResponseSchema,
  ResumeEnvironmentRequestSchema,
  ResumeEnvironmentResponseSchema,
  ResumeOutcome,
  SetEnvironmentIdleSuspendRequestSchema,
  SetEnvironmentWakeOnSendRequestSchema,
} from "../../../environments/v1/environment_service_pb";
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
  target: EnvironmentTarget,
  sessionId: string,
  spec: SessionSpec,
  setupScript?: string | null
): Promise<SessionOpening> {
  const request = create(OpenSessionRequestSchema, {
    name: target.name,
    sessionId,
    sandboxUid: target.uid,
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

export async function getEnvironmentRuntimeStatus(target: EnvironmentTarget): Promise<EnvironmentRuntimeStatus> {
  const request = create(GetEnvironmentRuntimeStatusRequestSchema, { name: target.name, sandboxUid: target.uid });
  const response = await fetchWithLogin(
    `/sandboxes/${encodeURIComponent(request.name)}/runner-status?sandbox_uid=${encodeURIComponent(request.sandboxUid)}`,
    {
      headers: { Accept: "application/json" },
    }
  );
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

const environmentPath = (target: EnvironmentTarget): string =>
  `/upstream/environments/v1/environments/${encodeURIComponent(target.namespace)}/${encodeURIComponent(target.name)}`;

async function environmentJSON(path: string, init?: RequestInit): Promise<JsonObject> {
  const response = await fetchWithLogin(path, init);
  const body: unknown = await response.json();
  if (!response.ok) throw new HttpError(response, body);
  if (typeof body !== "object" || body === null || Array.isArray(body))
    throw new Error("Invalid EnvironmentService response");
  return body as JsonObject;
}

function environmentPost(method: string, body: JsonObject): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export async function listEnvironmentTemplates(): Promise<string[]> {
  return fromJson(
    ListEnvironmentTemplatesResponseSchema,
    await environmentJSON("/upstream/environments/v1/environment-templates")
  ).names;
}

export async function createEnvironment(request: CreateEnvironmentRequest): Promise<EnvironmentTarget> {
  const body = toJson(CreateEnvironmentRequestSchema, request) as JsonObject;
  const target = fromJson(
    EnvironmentTargetSchema,
    await environmentJSON("/upstream/environments/v1/environments", environmentPost("POST", body))
  );
  if (!target.namespace || !target.uid || target.name !== request.name)
    throw new Error("EnvironmentService returned an invalid created target");
  return target;
}

export async function suspendEnvironment(target: EnvironmentTarget): Promise<void> {
  const request = create(EnvironmentMutationRequestSchema, { target });
  await environmentJSON(
    `${environmentPath(target)}:suspend`,
    environmentPost("POST", toJson(EnvironmentMutationRequestSchema, request) as JsonObject)
  );
}

export async function resumeSandbox(target: EnvironmentTarget): Promise<void> {
  const request = create(ResumeEnvironmentRequestSchema, { target });
  const response = fromJson(
    ResumeEnvironmentResponseSchema,
    await environmentJSON(
      `${environmentPath(target)}:resume`,
      environmentPost("POST", toJson(ResumeEnvironmentRequestSchema, request) as JsonObject)
    )
  );
  if (
    response.target?.namespace !== target.namespace ||
    response.target.name !== target.name ||
    response.target.uid !== target.uid
  )
    throw new Error("EnvironmentService returned a different target");
  if (response.outcome === ResumeOutcome.STATE_CHANGED)
    throw new Error("Environment state changed; refresh and try again");
  if (response.outcome !== ResumeOutcome.REQUESTED && response.outcome !== ResumeOutcome.ALREADY_RUNNING)
    throw new Error("EnvironmentService returned no resume outcome");
}

export async function deleteEnvironment(target: EnvironmentTarget): Promise<void> {
  await environmentJSON(`${environmentPath(target)}?target.uid=${encodeURIComponent(target.uid)}`, {
    method: "DELETE",
  });
}

export async function setEnvironmentWakeOnSend(target: EnvironmentTarget, wakeOnSend: boolean): Promise<void> {
  const request = create(SetEnvironmentWakeOnSendRequestSchema, { target, wakeOnSend });
  await environmentJSON(
    `${environmentPath(target)}:wake-policy`,
    environmentPost("PATCH", toJson(SetEnvironmentWakeOnSendRequestSchema, request) as JsonObject)
  );
}

export async function setEnvironmentIdleSuspend(target: EnvironmentTarget, seconds: number | null): Promise<void> {
  const request = create(SetEnvironmentIdleSuspendRequestSchema, {
    target,
    idleSuspendAfter: seconds === null ? undefined : create(DurationSchema, { seconds: BigInt(seconds) }),
  });
  await environmentJSON(
    `${environmentPath(target)}:idle-suspend`,
    environmentPost("PATCH", toJson(SetEnvironmentIdleSuspendRequestSchema, request) as JsonObject)
  );
}
