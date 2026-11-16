import { durationMs, timestampDate } from "@bufbuild/protobuf/wkt";

import type { AutosuspendBlocker } from "../../../proto/environment_lifecycle_pb";
import { SandboxPhase, type EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";

import { sandboxReady, sandboxStarting, sandboxSuspended } from "../sandbox_status";

export interface AutosuspendHint {
  text: string;
  deadline: string | null;
  blockers?: AutosuspendBlocker[];
}

const SECONDS_PER_MINUTE = 60;

export const AUTOSUSPEND_BLOCKER_LABELS: Record<string, string> = {
  ActiveTurn: "Turn running",
  PendingCommand: "Command pending",
  Setup: "Setup running",
  Activation: "Session starting",
  Bootstrap: "Sandbox bootstrap running",
  Admission: "Command being admitted",
  ExecutorRelay: "Executor connected",
};

function compactDuration(seconds: number): string {
  const minutes = Math.ceil(seconds / SECONDS_PER_MINUTE);
  const days = Math.floor(minutes / (24 * 60));
  const remainingHours = Math.floor((minutes % (24 * 60)) / 60);
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (days > 0) {
    const parts = [`${days}d`];
    if (remainingHours > 0) parts.push(`${remainingHours}h`);
    if (remainingMinutes > 0) parts.push(`${remainingMinutes}m`);
    return parts.join(" ");
  }
  if (hours === 0) return `${minutes}m`;
  return remainingMinutes === 0 ? `${hours}h` : `${hours}h ${remainingMinutes}m`;
}

/** The sandbox inventory is authoritative for this hint; stale snapshots cannot tell the reader when it will act. */
export function autosuspendHint(
  sandbox: EnvironmentDescriptor | undefined,
  fresh: boolean,
  now: number
): AutosuspendHint | null {
  if (!sandbox) return null;
  if (!fresh) return { text: "Autosuspend timing unavailable", deadline: null };

  if (sandbox.observedState?.phase === SandboxPhase.SUSPENDING) {
    return {
      text: `Sandbox ${sandbox.observedState?.suspendedBy === "Idle" ? "suspending (idle)" : "suspending"}`,
      deadline: null,
    };
  }
  if (sandboxSuspended(sandbox)) {
    const reason =
      sandbox.observedState?.suspendedBy === "Idle"
        ? " (idle)"
        : sandbox.observedState?.suspendedBy === "Requested"
          ? " (on request)"
          : "";
    return { text: `Sandbox suspended${reason}`, deadline: null };
  }

  const duration = sandbox.configuration?.idleSuspendAfter;
  if (duration === undefined) return { text: "Autosuspend off", deadline: null };
  const suspendAfter = durationMs(duration) / 1000;
  if (!Number.isFinite(suspendAfter) || suspendAfter < 60) {
    return { text: "Autosuspend timing unavailable", deadline: null };
  }

  const timestamp = sandbox.observedState?.idleSuspendAt;
  if (sandbox.observedState?.phase === SandboxPhase.RUNNING) {
    const blockers = sandbox.observedState?.autosuspendBlockers ?? [];
    const first = blockers[0];
    if (first) {
      const more = blockers.length > 1 ? ` · +${blockers.length - 1}` : "";
      return {
        text: `Autosuspend waiting: ${AUTOSUSPEND_BLOCKER_LABELS[first.reason] ?? first.reason}${more}`,
        deadline: null,
        blockers,
      };
    }
  }

  if (sandboxStarting(sandbox)) return { text: "Autosuspend waiting: Sandbox starting", deadline: null };
  if (sandbox.observedState?.phase !== SandboxPhase.RUNNING || !sandboxReady(sandbox)) {
    return { text: "Autosuspend timing unavailable", deadline: null };
  }
  if (timestamp === undefined) {
    return { text: `Autosuspend after ${compactDuration(suspendAfter)} idle`, deadline: null };
  }

  const deadlineDate = timestampDate(timestamp);
  const deadlineMs = deadlineDate.getTime();
  if (!Number.isFinite(deadlineMs)) return { text: "Autosuspend timing unavailable", deadline: null };
  const deadline = deadlineDate.toISOString();
  if (deadlineMs <= now) return { text: "Autosuspend pending", deadline };

  return { text: `Sandbox autosuspends in ~${compactDuration((deadlineMs - now) / 1000)}`, deadline };
}
