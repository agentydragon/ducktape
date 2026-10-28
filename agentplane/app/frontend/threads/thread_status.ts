/** One status decision for a Thread, shared by every page that lists Threads from a live snapshot. */
import type { SandboxView, ThreadView } from "../api/schema";
import type { Live } from "../live";
import type { ThreadTabStatus } from "../tab_metadata";
import { sandboxReady, sandboxSummary, suspensionReason } from "../sandbox_status";
import { turnErrorLabel } from "./history_rows";

/** Whether the stream is current and its Thread updates are being heard, so a snapshot's Thread states are live. */
export function snapshotFresh(live: Live<{ updates_connected: boolean }>): boolean {
  return live.stream.standing === "current" && live.health?.fresh === true && live.snapshot?.updates_connected === true;
}

export function archiveBlockReason(thread: ThreadView): string | null {
  if (thread.active_turn_id) return "Wait for the active turn to finish before archiving";
  if (thread.setup_state === "SETUP_STATE_RUNNING") return "Wait for setup to finish before archiving";
  return null;
}

export function threadStatusFromSnapshot(
  thread: ThreadView | undefined,
  sandbox: SandboxView | undefined,
  fresh: boolean
): ThreadTabStatus {
  if (!thread || !fresh) return { kind: "inactive", label: "No live harness confirmed", tabLabel: "Unknown" };
  if (thread.archived) return { kind: "archived", label: "Thread archived", tabLabel: "Archived" };
  if (sandbox && sandboxSummary(sandbox).kind === "suspended") {
    const reason = suspensionReason(sandbox);
    return {
      kind: "suspended",
      label: reason === null ? "Sandbox suspended" : `Sandbox suspended (${reason})`,
      tabLabel: "Suspended",
    };
  }
  if (!sandboxReady(sandbox)) return { kind: "inactive", label: "Sandbox unavailable", tabLabel: "Unavailable" };
  if (thread.feed_status === "failed")
    return { kind: "failed", label: "Runner feed failed", tabLabel: "Runner failed" };
  // The harness starts on first input; a pending launch outcome can mean the Session is still dormant.
  if (thread.harness_state !== "HARNESS_STATE_RUNNING") {
    if (thread.setup_state === "SETUP_STATE_RUNNING")
      return { kind: "inactive", label: "Setup script running", tabLabel: "Setting up" };
  }
  // A dormant harness can be down while its Runner feed remains attached to the Session.
  const harnessStopped = thread.harness_state === "HARNESS_STATE_STOPPED";
  if (thread.feed_status === "ended" && !harnessStopped)
    return { kind: "inactive", label: "Runner feed ended", tabLabel: "Ended" };
  if (thread.feed_status !== "active" && thread.feed_status !== "ended" && !harnessStopped)
    return { kind: "inactive", label: "No live harness confirmed", tabLabel: "Starting" };
  if (harnessStopped) return { kind: "stopped", label: "Harness not running", tabLabel: "Stopped" };
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
