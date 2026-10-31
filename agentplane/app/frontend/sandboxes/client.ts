import { toJson, type JsonObject } from "@bufbuild/protobuf";

import type { components } from "../schema";
import { api, ok } from "../client";
import { SessionSpecSchema, type SessionSpec } from "../../../proto/runner_pb";

type Harness = NonNullable<components["schemas"]["ThreadView"]["harness"]>;
/** The runner's protobuf JSON status; this route carries generated proto data as a raw JSON object. */
export type RunnerLifecycleBlockerView = {
  kind:
    | "RUNNER_LIFECYCLE_BLOCKER_KIND_UNSPECIFIED"
    | "RUNNER_LIFECYCLE_BLOCKER_KIND_ADMISSIONS"
    | "RUNNER_LIFECYCLE_BLOCKER_KIND_SESSION_HALT"
    | "RUNNER_LIFECYCLE_BLOCKER_KIND_SESSION_PROCESS"
    | "RUNNER_LIFECYCLE_BLOCKER_KIND_CODEX_APP_SERVER"
    | "RUNNER_LIFECYCLE_BLOCKER_KIND_CODEX_EXEC_SERVER"
    | "RUNNER_LIFECYCLE_BLOCKER_KIND_BOOTSTRAP"
    | "RUNNER_LIFECYCLE_BLOCKER_KIND_PROCESS_CLEANUP"
    | "RUNNER_LIFECYCLE_BLOCKER_KIND_SESSION_INSPECTION"
    | "RUNNER_LIFECYCLE_BLOCKER_KIND_JOURNAL_SEAL"
    | "RUNNER_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT";
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
export type RunnerLifecycleView = {
  desired_mode:
    | "RUNNER_LIFECYCLE_MODE_UNSPECIFIED"
    | "RUNNER_LIFECYCLE_MODE_RUNNING"
    | "RUNNER_LIFECYCLE_MODE_QUIESCING"
    | "RUNNER_LIFECYCLE_MODE_CLOSING";
  desired_generation: string;
  progress_generation: string;
  progress_available: boolean;
  progress_current: boolean;
  stage:
    | "RUNNER_LIFECYCLE_STAGE_UNSPECIFIED"
    | "RUNNER_LIFECYCLE_STAGE_RUNNING"
    | "RUNNER_LIFECYCLE_STAGE_DRAINING"
    | "RUNNER_LIFECYCLE_STAGE_HALTING"
    | "RUNNER_LIFECYCLE_STAGE_STOPPING"
    | "RUNNER_LIFECYCLE_STAGE_FENCING"
    | "RUNNER_LIFECYCLE_STAGE_SEALING"
    | "RUNNER_LIFECYCLE_STAGE_FLUSHING"
    | "RUNNER_LIFECYCLE_STAGE_QUIESCED"
    | "RUNNER_LIFECYCLE_STAGE_CLOSED";
  error: string;
  blocker: RunnerLifecycleBlockerView | null;
};
export type RunnerStatusView = {
  runner_boot_id: string;
  /** Missing on older Runner processes; protobuf JSON uses decimal strings for uint64 values. */
  lifecycle?: RunnerLifecycleView | null;
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
export type SessionOpening = {
  sessionId: string;
  sandbox: string;
  sandboxUid: string;
  state: string;
  reason?: string;
};

/**
 * Create the Session `sessionId`, a fresh UUID, names. On an ambiguous response, look for its
 * Thread (`findThread`); never re-run bootstrap implicitly.
 */
export async function openSession(
  sandbox: string,
  sandboxUID: string,
  sessionId: string,
  spec: SessionSpec,
  setupScript?: string
): Promise<SessionOpening> {
  const data = ok(
    await api.POST("/sandboxes/{name}/sessions", {
      params: { path: { name: sandbox } },
      body: {
        session_id: sessionId,
        sandbox_uid: sandboxUID,
        spec: toJson(SessionSpecSchema, spec) as JsonObject,
        setup_script: setupScript,
      },
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

export async function getRunnerStatus(sandbox: string): Promise<RunnerStatusView> {
  return ok(
    await api.GET("/sandboxes/{name}/runner-status", {
      params: { path: { name: sandbox } },
    })
  ) as RunnerStatusView;
}

/** Asks for the Sandbox to run; the watch behind the live streams brings back the change. */
export async function resumeSandbox(name: string): Promise<void> {
  ok(await api.POST("/sandboxes/{name}/resume", { params: { path: { name } } }));
}
