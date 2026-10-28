import type { SandboxView } from "../schema";
import { sandboxReady, sandboxStarting, sandboxSuspended } from "../sandbox_status";

export interface AutosuspendHint {
  text: string;
  deadline: string | null;
  blockers?: SandboxView["autosuspend_blockers"];
}

const SECONDS_PER_MINUTE = 60;

export const AUTOSUSPEND_BLOCKER_LABELS: Record<
  NonNullable<SandboxView["autosuspend_blockers"]>[number]["reason"],
  string
> = {
  ActiveTurn: "Turn running",
  PendingCommand: "Command pending",
  Setup: "Setup running",
  Activation: "Session starting",
  Bootstrap: "Sandbox bootstrap running",
  Admission: "Command being admitted",
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
export function autosuspendHint(sandbox: SandboxView | undefined, fresh: boolean, now: number): AutosuspendHint | null {
  if (!sandbox) return null;
  if (!fresh) return { text: "Autosuspend timing unavailable", deadline: null };

  if (sandbox.phase === "Suspending") {
    return { text: `Sandbox ${sandbox.suspended_by === "Idle" ? "suspending (idle)" : "suspending"}`, deadline: null };
  }
  if (sandboxSuspended(sandbox)) {
    const reason =
      sandbox.suspended_by === "Idle" ? " (idle)" : sandbox.suspended_by === "Requested" ? " (on request)" : "";
    return { text: `Sandbox suspended${reason}`, deadline: null };
  }

  const suspendAfter = sandbox.idle_suspend_after_seconds;
  if (suspendAfter === null || suspendAfter === undefined) return { text: "Autosuspend off", deadline: null };
  if (!Number.isFinite(suspendAfter) || suspendAfter < 60) {
    return { text: "Autosuspend timing unavailable", deadline: null };
  }

  const deadline = sandbox.idle_suspend_at;
  if (sandbox.phase === "Running") {
    const blockers = sandbox.autosuspend_blockers ?? [];
    const first = blockers[0];
    if (first) {
      const more = blockers.length > 1 ? ` · +${blockers.length - 1}` : "";
      return {
        text: `Autosuspend waiting: ${AUTOSUSPEND_BLOCKER_LABELS[first.reason]}${more}`,
        deadline: null,
        blockers,
      };
    }
  }

  if (sandboxStarting(sandbox)) return { text: "Autosuspend waiting: Sandbox starting", deadline: null };
  if (sandbox.phase !== "Running" || !sandboxReady(sandbox)) {
    return { text: "Autosuspend timing unavailable", deadline: null };
  }
  if (deadline === null || deadline === undefined) {
    return { text: `Autosuspend after ${compactDuration(suspendAfter)} idle`, deadline: null };
  }

  const deadlineMs = Date.parse(deadline);
  if (!Number.isFinite(deadlineMs)) return { text: "Autosuspend timing unavailable", deadline: null };
  if (deadlineMs <= now) return { text: "Autosuspend pending", deadline };

  return { text: `Sandbox autosuspends in ~${compactDuration((deadlineMs - now) / 1000)}`, deadline };
}
