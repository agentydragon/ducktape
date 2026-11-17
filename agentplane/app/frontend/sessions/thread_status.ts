import {
  SessionActivity_EnvironmentDown_Cause,
  SessionActivity_Unknown_Reason,
  SessionStatus_Materialization as Materialization,
  SessionStatus_RequiredEnvironment as RequiredEnvironment,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { HarnessAttachment_State, type Session } from "../../../harness_host/harness_host_pb";
import { SetupState } from "../../../proto/session_pb";
import type { Live } from "../transport/live";
import type { ThreadTabStatus } from "../shell/tab_metadata";
import type { ThreadView } from "./client";
import { turnErrorLabel } from "./history_rows";

/** Activity is current only after Dispatcher's checkpoint, and becomes unknown as soon as its watch drops. */
export function sessionActivityFresh(live: Live<{ activity_current: boolean }>): boolean {
  return live.snapshot?.activity_current === true;
}

export function observedSession(thread: ThreadView | undefined, fresh: boolean): Session | undefined {
  const activity = thread?.session?.status?.activity?.kind;
  if (!fresh || activity?.case !== "observed" || activity.value.sessionId !== thread?.metadata.threadId)
    return undefined;
  const session = activity.value;
  if (session.host.case === undefined || session.host.case !== thread.session?.placement?.kind.case) return undefined;
  if (
    ![
      SetupState.NOT_REQUIRED,
      SetupState.RUNNING,
      SetupState.SUCCEEDED,
      SetupState.FAILED,
      SetupState.INTERRUPTED,
    ].includes(session.host.value.setup)
  )
    return undefined;
  if (
    session.host.case === "environmentHosted" &&
    ![HarnessAttachment_State.DETACHED, HarnessAttachment_State.STARTING, HarnessAttachment_State.ATTACHED].includes(
      session.host.value.harness?.state ?? HarnessAttachment_State.UNSPECIFIED
    )
  )
    return undefined;
  return session;
}

export function archiveBlockReason(thread: ThreadView, fresh: boolean): string | null {
  const status = threadStatusFromSnapshot(thread, fresh);
  if (status.kind === "unknown") return "Wait for Session activity to be known before archiving";
  const session = observedSession(thread, fresh);
  if (!session) return null; // A confirmed EnvironmentDown has no live host work.
  if (session.activeTurnId) return "Wait for the active turn to finish before archiving";
  if (session.pendingCommands > 0) return "Wait for pending commands to finish before archiving";
  if (session.host.value?.setup === SetupState.RUNNING) return "Wait for setup to finish before archiving";
  if (
    session.host.case === "environmentHosted" &&
    session.host.value.harness?.state === HarnessAttachment_State.STARTING
  )
    return "Wait for the harness to start before archiving";
  return null;
}

const unknown = (reason: string): ThreadTabStatus => ({
  kind: "unknown",
  label: `Session activity unknown · ${reason}`,
  tabLabel: "Unknown",
});

/** Dispatcher owns current activity. History contributes only the last completed turn's outcome. */
export function threadStatusFromSnapshot(thread: ThreadView | undefined, fresh: boolean): ThreadTabStatus {
  if (thread?.metadata.archived) return { kind: "archived", label: "Thread archived", tabLabel: "Archived" };
  if (!fresh) return unknown("Dispatcher watch is not current");
  const activity = thread?.session?.status?.activity?.kind;
  if (activity?.case === "unknown")
    return unknown(
      activity.value.reason === SessionActivity_Unknown_Reason.NOT_YET_OBSERVED
        ? "not yet observed"
        : "host observation lost"
    );
  if (activity?.case === "environmentDown") {
    switch (activity.value.cause) {
      case SessionActivity_EnvironmentDown_Cause.SUSPENDED:
        return { kind: "suspended", label: "Environment down · Sandbox suspended", tabLabel: "Environment down" };
      case SessionActivity_EnvironmentDown_Cause.DELETED:
        return { kind: "environment_down", label: "Environment down · Sandbox deleted", tabLabel: "Environment down" };
      default:
        return unknown("unrecognised Environment state");
    }
  }
  const session = observedSession(thread, fresh);
  if (!session) return unknown("no compatible host observation");
  if (session.activeTurnId) return { kind: "running", label: "Turn running", tabLabel: "Running" };
  if (session.pendingCommands > 0)
    return {
      kind: "pending",
      label: `${session.pendingCommands} command${session.pendingCommands === 1 ? "" : "s"} pending`,
      tabLabel: "Pending",
    };
  const host = session.host;
  if (host.value?.setup === SetupState.RUNNING)
    return { kind: "setting_up", label: "Setup script running", tabLabel: "Setting up" };
  if (host.value?.setup === SetupState.FAILED || host.value?.setup === SetupState.INTERRUPTED)
    return {
      kind: "failed",
      label: host.value.setup === SetupState.FAILED ? "Setup script failed" : "Setup script interrupted",
      tabLabel: "Setup failed",
    };
  if (host.case === "environmentHosted") {
    if (host.value.harness?.state === HarnessAttachment_State.STARTING)
      return { kind: "pending", label: "Harness starting", tabLabel: "Starting" };
    if (host.value.harness?.state === HarnessAttachment_State.DETACHED)
      return host.value.harness.failureCursor !== undefined
        ? { kind: "failed", label: "Harness not attached · last launch or process failed", tabLabel: "Harness failed" }
        : { kind: "stopped", label: "Harness not attached", tabLabel: "Dormant" };
  }
  if (host.case === "centralSamplerV1" && host.value.blocked)
    return {
      kind: "blocked",
      label: `Session blocked${host.value.blocked.detail ? ` · ${host.value.blocked.detail}` : ""}`,
      tabLabel: "Blocked",
    };
  if (thread?.summary.lastTurnStatus !== undefined) {
    const outcome = turnErrorLabel(thread.summary.lastTurnStatus);
    if (outcome.kind === "error") return { kind: "turn_error", label: outcome.label, tabLabel: outcome.label };
    if (outcome.kind === "unrecognised") return unknown("unrecognised last turn outcome");
  }
  return {
    kind: "idle",
    label: host.case === "environmentHosted" ? "Harness attached · ready for input" : "Session ready for input",
    tabLabel: "Ready",
  };
}

/** Dispatcher-owned facts; these labels make no claim about a native harness's running state. */
export function sessionMaterializationLabel(thread: ThreadView): string | null {
  const status = thread.session?.status;
  if (!status) return null;
  if (
    thread.summary.attached &&
    status.materialization === Materialization.SESSION_MATERIALIZATION_ACCEPTED &&
    status.requiredEnvironment !== RequiredEnvironment.DELETED
  )
    return null;
  const state =
    {
      [Materialization.SESSION_MATERIALIZATION_UNSPECIFIED]: "Materialization unspecified",
      [Materialization.SESSION_MATERIALIZATION_WAITING]: "Waiting for placement",
      [Materialization.SESSION_MATERIALIZATION_IN_PROGRESS]: "Materializing Session",
      [Materialization.SESSION_MATERIALIZATION_ACCEPTED]: "Session materialized",
      [Materialization.SESSION_MATERIALIZATION_FAILED]: "Session materialization failed",
      [Materialization.SESSION_MATERIALIZATION_UNKNOWN]: "Session materialization unknown",
    }[status.materialization] ?? "Session materialization unknown";
  const environment =
    status.requiredEnvironment === RequiredEnvironment.DELETED ? " · Required Environment deleted" : "";
  return state + environment + (status.reason ? ` · ${status.reason}` : "");
}
