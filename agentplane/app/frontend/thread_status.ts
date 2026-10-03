/** One status decision for the shell's shared /live/threads snapshot. */
import type { SandboxView, ThreadView } from "./client";
import type { Live, ThreadsSnapshot } from "./live";
import type { ThreadTabStatus } from "./tab_metadata";
import { sandboxReady } from "./sandbox_status";

export function snapshotFresh(live: Live<ThreadsSnapshot>): boolean {
  return live.stream.standing === "current" && live.health?.fresh === true && live.snapshot?.updates_connected === true;
}

export function threadStatusFromSnapshot(
  thread: ThreadView | undefined,
  sandbox: SandboxView | undefined,
  fresh: boolean
): ThreadTabStatus {
  if (!thread || !fresh) return { color: "gray", label: "No live harness confirmed", tabLabel: "Unknown" };
  if (thread.archived) return { color: "gray", label: "Thread archived", tabLabel: "Archived" };
  if (!sandboxReady(sandbox)) return { color: "gray", label: "Sandbox unavailable", tabLabel: "Unavailable" };
  if (thread.feed_status === "failed") return { color: "red", label: "Runner feed failed", tabLabel: "Runner failed" };
  if (thread.feed_status === "ended") return { color: "gray", label: "Runner feed ended", tabLabel: "Ended" };
  if (thread.feed_status !== "active")
    return { color: "gray", label: "No live harness confirmed", tabLabel: "Starting" };
  if (thread.harness_state !== "HARNESS_STATE_RUNNING")
    return { color: "gray", label: "Harness not running", tabLabel: "Stopped" };
  const activeTurn = Boolean(thread.active_turn_id);
  return {
    color: "green",
    label: activeTurn ? "Turn running · Runner feed active · harness running" : "Runner feed active · harness running",
    tabLabel: activeTurn ? "Running" : "Ready",
    pulse: activeTurn,
  };
}
