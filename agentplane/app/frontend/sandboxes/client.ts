import { create, fromJson, toJson, type JsonObject } from "@bufbuild/protobuf";
import { DurationSchema } from "@bufbuild/protobuf/wkt";

import { fetchJson, HttpError } from "../transport/client";
import { createIdentity, deleteIdentity, grantIdentityPolicyBinding } from "../identities/client";
import { EnvironmentTargetSchema, type EnvironmentTarget } from "../../../proto/identity_pb";
import {
  CreateEnvironmentRequestSchema,
  type CreateEnvironmentRequest,
  EnvironmentMutationRequestSchema,
  EnvironmentIdentityRecordSchema,
  GetRuntimeDiagnosticsResponseSchema,
  ListEnvironmentTemplatesResponseSchema,
  ResumeEnvironmentRequestSchema,
  ResumeEnvironmentResponseSchema,
  ResumeOutcome,
  SetEnvironmentIdleSuspendRequestSchema,
  SetEnvironmentIdentityRequestSchema,
  SetEnvironmentWakeOnSendRequestSchema,
} from "../../../environments/v1/environment_service_pb";
import type { EnvironmentRuntimeStatus } from "../../../proto/session_pb";

export async function getEnvironmentRuntimeStatus(target: EnvironmentTarget): Promise<EnvironmentRuntimeStatus> {
  if (!target.namespace || !target.name || !target.uid) throw new Error("Complete Environment target is required");
  const response = fromJson(
    GetRuntimeDiagnosticsResponseSchema,
    await environmentJSON(
      `${environmentPath(target)}:runtime-diagnostics?target.uid=${encodeURIComponent(target.uid)}`,
      { headers: { Accept: "application/json" } }
    )
  );
  if (
    response.target?.namespace !== target.namespace ||
    response.target.name !== target.name ||
    response.target.uid !== target.uid ||
    !response.incarnationUid ||
    !response.observedAt ||
    !response.status?.environmentRuntimeBootId
  )
    throw new Error("EnvironmentService returned incomplete or mismatched Runtime evidence");
  return response.status;
}

const environmentPath = (target: EnvironmentTarget): string =>
  `/upstream/environments/v1/environments/${encodeURIComponent(target.namespace)}/${encodeURIComponent(target.name)}`;

async function environmentJSON(path: string, init?: RequestInit): Promise<JsonObject> {
  const body = await fetchJson(path, init);
  if (typeof body !== "object" || body === null || Array.isArray(body))
    throw new Error("Invalid EnvironmentService response");
  return body;
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

/**
 * Launches a Sandbox as three operations: the Identity it acts as, that Identity's PolicySets, then
 * the Sandbox naming it. If a step is refused, the Identity is deleted, and with it its grant; a
 * failure whose outcome is unknown leaves it for the Identities page.
 */
export async function launchEnvironment(
  request: CreateEnvironmentRequest,
  policySets: string[]
): Promise<EnvironmentTarget> {
  const identity = await createIdentity(request.name, `Agent of Sandbox ${request.name}, launched from the app.`);
  try {
    if (policySets.length > 0) await grantIdentityPolicyBinding(identity, policySets);
    return await createEnvironment({ ...request, identity: request.name });
  } catch (failure) {
    if (
      failure instanceof HttpError &&
      failure.status >= 400 &&
      failure.status < 500 &&
      failure.status !== 408 &&
      failure.status !== 429
    ) {
      await deleteIdentity(identity);
    }
    throw failure;
  }
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

/** Only the current Sandbox-owned credential survives; older acting identities are not retained. */
export async function getEnvironmentIdentityRecord(target: EnvironmentTarget): Promise<string | null> {
  const record = fromJson(
    EnvironmentIdentityRecordSchema,
    await environmentJSON(`${environmentPath(target)}:identity-record?target.uid=${encodeURIComponent(target.uid)}`)
  );
  return record.identity?.name || null;
}

export async function setEnvironmentIdentity(target: EnvironmentTarget, identityName: string | null): Promise<void> {
  const request = create(SetEnvironmentIdentityRequestSchema, { target, identityName: identityName ?? "" });
  await environmentJSON(
    `${environmentPath(target)}:identity`,
    environmentPost("PATCH", toJson(SetEnvironmentIdentityRequestSchema, request) as JsonObject)
  );
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
