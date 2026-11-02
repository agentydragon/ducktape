import { create, fromJson, toJson, type JsonObject } from "@bufbuild/protobuf";
import { ValueSchema } from "@bufbuild/protobuf/wkt";

import type { components } from "../schema";
import { api, ok, fetchWithLogin, HttpError } from "../client";
import { SessionSpecSchema, type SessionSpec } from "../../../proto/session_pb";
import { OpenSessionRequestSchema, GetSessionOpeningRequestSchema } from "../../session_opening/session_opening_pb";
import { SessionOpeningSchema, type SessionOpening } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";

type Harness = NonNullable<components["schemas"]["ThreadView"]["harness"]>;
/** The runner's protobuf JSON status; this route carries generated proto data as a raw JSON object. */
export type EnvironmentRuntimeLifecycleBlockerView = {
  kind:
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_UNSPECIFIED"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_ADMISSIONS"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_HALT"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_PROCESS"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_CODEX_APP_SERVER"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_CODEX_EXEC_SERVER"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_BOOTSTRAP"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_PROCESS_CLEANUP"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_INSPECTION"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_JOURNAL_SEAL"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT";
  resource_type: string;
  resource_id: string;
  process_id: string;
  resource_present: boolean;
  admissions: string;
  journal_seal_present: boolean;
  journal_seal_uid: string;
  expected_journal_seal_uid: string;
  journal_seal_cursor: string;
  acknowledgement_present: boolean;
  acknowledgement_sealed: boolean;
  acknowledgement_cursor: string;
  finalizers_pending: boolean;
  error: string;
};
export type EnvironmentRuntimeLifecycleView = {
  desired_mode:
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_UNSPECIFIED"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_RUNNING"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_CLOSING";
  desired_generation: string;
  progress_generation: string;
  progress_available: boolean;
  progress_current: boolean;
  stage:
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_UNSPECIFIED"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_RUNNING"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_DRAINING"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_HALTING"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_STOPPING"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FENCING"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_SEALING"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_QUIESCED"
    | "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_CLOSED";
  error: string;
  blocker: EnvironmentRuntimeLifecycleBlockerView | null;
};
export type EnvironmentRuntimeStatusView = {
  environment_runtime_boot_id: string;
  /** Missing on older Runner processes; protobuf JSON uses decimal strings for uint64 values. */
  lifecycle?: EnvironmentRuntimeLifecycleView | null;
  processes: {
    process_id: string;
    pid: number | string | null;
    harness: Harness | "HARNESS_UNSPECIFIED";
    state: "PROCESS_STATE_STARTING" | "PROCESS_STATE_RUNNING" | "PROCESS_STATE_STOPPING" | "PROCESS_STATE_UNSPECIFIED";
    assigned_session_ids: string[];
  }[];
  sessions: {
    session_id: string;
    process_id: string | null;
    native_session_id: string | null;
    execution_state:
      | "SESSION_EXECUTION_STATE_DORMANT"
      | "SESSION_EXECUTION_STATE_PREPARING"
      | "SESSION_EXECUTION_STATE_READY"
      | "SESSION_EXECUTION_STATE_FAILED"
      | "SESSION_EXECUTION_STATE_UNSPECIFIED";
    active_turn_id: string | null;
    failure_cursor: number | string | null;
    busy: boolean;
  }[];
};
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

export async function getEnvironmentRuntimeStatus(sandbox: string): Promise<EnvironmentRuntimeStatusView> {
  return ok(
    await api.GET("/sandboxes/{name}/runner-status", {
      params: { path: { name: sandbox } },
    })
  ) as EnvironmentRuntimeStatusView;
}

/** Asks for the Sandbox to run; the watch behind the live streams brings back the change. */
export async function resumeSandbox(name: string): Promise<void> {
  ok(await api.POST("/sandboxes/{name}/resume", { params: { path: { name } } }));
}
