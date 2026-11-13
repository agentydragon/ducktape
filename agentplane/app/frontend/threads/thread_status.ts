import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";
/** One status decision for a Thread, shared by every page that lists Threads from a live snapshot. */
import type { ThreadView } from "./client";
import { FeedStatus } from "../../../history_service/api/history/history_service_pb";
import { HarnessState, SetupState } from "../../../proto/session_pb";
import type { Live } from "../live";
import type { ThreadTabStatus } from "../tab_metadata";
import { sandboxReady, sandboxSummary, suspensionReason } from "../sandbox_status";
import { attachedHarness, harnessBehavior } from "../harness_behavior";
import { turnErrorLabel } from "./history_rows";

/** Whether the stream is current and its Thread updates are being heard, so a snapshot's Thread states are live. */
export function snapshotFresh(live: Live<{ updates_connected: boolean }>): boolean {
  return (
    live.stream.standing === "current" && live.health?.healthy === true && live.snapshot?.updates_connected === true
  );
}

export function archiveBlockReason(thread: ThreadView): string | null {
  if (thread.summary.attached?.activeTurnId) return "Wait for the active turn to finish before archiving";
  if (thread.summary.attached?.setupState === SetupState.RUNNING) return "Wait for setup to finish before archiving";
  return null;
}

export function threadStatusFromSnapshot(
  thread: ThreadView | undefined,
  sandbox: EnvironmentDescriptor | undefined,
  fresh: boolean
): ThreadTabStatus {
  if (!thread || !fresh) return { kind: "inactive", label: "No live harness confirmed", tabLabel: "Unknown" };
  if (thread.metadata.archived) return { kind: "archived", label: "Thread archived", tabLabel: "Archived" };
  const { commandsRequireReadySandbox } = harnessBehavior(attachedHarness(thread.summary.attached));
  if (commandsRequireReadySandbox && sandbox && sandboxSummary(sandbox).kind === "suspended") {
    const reason = suspensionReason(sandbox);
    return {
      kind: "suspended",
      label: reason === null ? "Sandbox suspended" : `Sandbox suspended (${reason})`,
      tabLabel: "Suspended",
    };
  }
  if (commandsRequireReadySandbox && !sandboxReady(sandbox))
    return { kind: "inactive", label: "Sandbox unavailable", tabLabel: "Unavailable" };
  const attachment = thread.summary.attached;
  const feedStatus =
    attachment && thread.summary.feedStatus !== FeedStatus.UNSPECIFIED ? thread.summary.feedStatus : null;
  const harnessState = attachment?.harnessState ?? HarnessState.UNSPECIFIED;
  const setupState = attachment?.setupState;
  if (feedStatus === FeedStatus.FAILED)
    return { kind: "failed", label: "Runner feed failed", tabLabel: "Runner failed" };
  // The harness starts on first input; a pending launch outcome can mean the Session is still dormant.
  if (harnessState !== HarnessState.RUNNING) {
    if (setupState === SetupState.RUNNING)
      return { kind: "inactive", label: "Setup script running", tabLabel: "Setting up" };
  }
  // A dormant harness can be down while its Runner feed remains attached to the Session.
  const harnessStopped = harnessState === HarnessState.STOPPED;
  if (feedStatus === FeedStatus.ENDED && !harnessStopped)
    return { kind: "inactive", label: "Runner feed ended", tabLabel: "Ended" };
  if (feedStatus !== FeedStatus.ACTIVE && feedStatus !== FeedStatus.ENDED && !harnessStopped)
    return { kind: "inactive", label: "No live harness confirmed", tabLabel: "Starting" };
  if (harnessStopped) return { kind: "stopped", label: "Harness not running", tabLabel: "Stopped" };
  if (harnessState !== HarnessState.RUNNING)
    return { kind: "stopped", label: "Harness not running", tabLabel: "Stopped" };
  if (attachment?.activeTurnId)
    return { kind: "running", label: "Turn running · Runner feed active · harness running", tabLabel: "Running" };
  if (thread.summary.lastTurnStatus !== undefined) {
    const turnError = turnErrorLabel(thread.summary.lastTurnStatus);
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
          thread.summary.lastTurnStatus
        );
        break;
      case "ordinary":
        break;
    }
  }
  return { kind: "idle", label: "Runner feed active · harness running", tabLabel: "Ready" };
}
