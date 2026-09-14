/**
 * The sidebar's grouping: `GET /threads/with-sandboxes`'s flat, newest-first Thread list folded
 * into one row per hosting Sandbox. A Thread's `sandbox` name missing from the response's
 * `sandboxes` map means that Sandbox is gone; its group still renders, read-only.
 */
import type { SandboxView, ThreadView } from "./client";

export interface ThreadGroup {
  /** Null while the Thread's target has not yet become a runner attachment. */
  sandboxName: string | null;
  /** The Sandbox's live view, or null for a target-only/deleted Thread. */
  sandbox: SandboxView | null;
  /** A target-only Thread is distinct from a Thread whose previously attached Sandbox was deleted. */
  starting: boolean;
  /** Newest first, matching the API's own order. */
  threads: ThreadView[];
}

/**
 * One group per Sandbox plus a `starting` group for target-only Threads. Groups follow the newest
 * Thread (`threads` arrives newest-first). Archived Threads, and a group left with none once
 * they're excluded, drop out entirely.
 */
export function groupThreads(
  threads: ThreadView[],
  sandboxes: Record<string, SandboxView>,
  includeArchived: boolean
): ThreadGroup[] {
  const groups = new Map<string | null, ThreadGroup>();
  for (const thread of threads) {
    if (!includeArchived && thread.archived) continue;
    let group = groups.get(thread.sandbox);
    if (!group) {
      group = {
        sandboxName: thread.sandbox,
        sandbox: thread.sandbox === null ? null : (sandboxes[thread.sandbox] ?? null),
        starting: thread.sandbox === null,
        threads: [],
      };
      groups.set(thread.sandbox, group);
    }
    group.threads.push(thread);
  }
  return [...groups.values()];
}

export function archivedCount(threads: ThreadView[]): number {
  return threads.filter((thread) => thread.archived).length;
}

/** Green while the thread's own harness is attached and running; gray otherwise (idle, or its
 * Sandbox suspended/deleted, which leaves no attached feed behind either). */
export function threadDotColor(thread: ThreadView): "ok" | "gray" {
  return thread.harness_state === "HARNESS_STATE_RUNNING" ? "ok" : "gray";
}
