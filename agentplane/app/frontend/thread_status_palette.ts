/** What a thread's status looks like. The in-page status indicator and the tab favicon both read this, so they cannot drift. */

/**
 * `running`: a turn is in flight, drawn as moving chevrons rather than a dot. `idle`: the harness is
 * live and waiting for input. `failed`: the runner feed failed. `inactive`: nothing live to show
 * (archived, Sandbox suspended or gone, harness stopped, feed ended, or not confirmed).
 */
export type ThreadStatusKind = "running" | "idle" | "failed" | "inactive";

// Mantine default-palette shades. `idle` is blue so it reads apart from both green (`running`) and the
// gray of a suspended Sandbox (`inactive`).
export const THREAD_STATUS_COLORS: Record<ThreadStatusKind, string> = {
  running: "#40c057", // green-6
  idle: "#4dabf7", // blue-4
  failed: "#fa5252", // red-6
  inactive: "#868e96", // gray-6
};

/** Time for the running chevrons to advance one chevron pitch to the right, for the in-page CSS animation and the favicon's frames alike. */
export const THREAD_STATUS_RUN_CYCLE_MS = 1_000;
