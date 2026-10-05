/** One status decision for the shell's shared /live/threads snapshot. */
import type { SandboxView, ThreadView } from "./client";
import type { Live, ThreadsSnapshot } from "./live";
import type { ThreadTabStatus } from "./tab_metadata";
import { sandboxReady } from "./sandbox_status";
import { turnErrorLabel } from "./threads/history_rows";

export function snapshotFresh(live: Live<ThreadsSnapshot>): boolean {
  return live.stream.standing === "current" && live.health?.fresh === true && live.snapshot?.updates_connected === true;
}

export function threadStatusFromSnapshot(
  thread: ThreadView | undefined,
  sandbox: SandboxView | undefined,
  fresh: boolean
): ThreadTabStatus {
  if (!thread || !fresh) return { kind: "inactive", label: "No live harness confirmed", tabLabel: "Unknown" };
  if (thread.archived) return { kind: "archived", label: "Thread archived", tabLabel: "Archived" };
  if (!sandboxReady(sandbox)) return { kind: "inactive", label: "Sandbox unavailable", tabLabel: "Unavailable" };
  if (thread.feed_status === "failed")
    return { kind: "failed", label: "Runner feed failed", tabLabel: "Runner failed" };
  // A stopped harness is down whether its feed is still attached or has ended: a stopped session has no
  // live entries, so a feed attached to it ends at once, and a shutdown settles there.
  const harnessStopped = thread.harness_state === "HARNESS_STATE_STOPPED";
  if (thread.feed_status === "ended" && !harnessStopped)
    return { kind: "inactive", label: "Runner feed ended", tabLabel: "Ended" };
  if (thread.feed_status !== "active" && thread.feed_status !== "ended")
    return { kind: "inactive", label: "No live harness confirmed", tabLabel: "Starting" };
  if (thread.harness_state !== "HARNESS_STATE_RUNNING")
    return { kind: "stopped", label: "Harness not running", tabLabel: "Stopped" };
  if (thread.active_turn_id)
    return { kind: "running", label: "Turn running · Runner feed active · harness running", tabLabel: "Running" };
  if (thread.last_turn_status) {
    const turnError = turnErrorLabel(thread.last_turn_status);
    switch (turnError.kind) {
      case "error":
        return {
          kind: "turn_error",
          label: `${turnError.label} · Runner feed active · harness running`,
          tabLabel: turnError.label,
        };
      case "unrecognised":
        console.warn(
          "thread_status: unrecognised last turn status, showing the thread as idle",
          thread.last_turn_status
        );
        break;
      case "ordinary":
        break;
    }
  }
  return { kind: "idle", label: "Runner feed active · harness running", tabLabel: "Ready" };
}
