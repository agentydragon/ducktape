/**
 * The sidebar's grouping: `GET /threads/with-sandboxes`'s flat, newest-first Thread list folded
 * into one row per hosting Sandbox. A Thread's `sandbox` name missing from the response's
 * `sandboxes` map means that Sandbox is gone; its group still renders, read-only.
 */
import type { SandboxView, ThreadView } from "./client";

export interface ThreadGroup {
  sandboxName: string;
  /** The Sandbox's live view, or null once it no longer exists. */
  sandbox: SandboxView | null;
  /** Newest first, matching the API's own order. */
  threads: ThreadView[];
}

/**
 * One group per Sandbox a visible Thread names, ordered by that group's newest Thread (`threads`
 * arrives newest-first, so the first Thread seen for a Sandbox fixes its group's position).
 * Archived Threads, and a Sandbox left with none once they're excluded, drop out entirely — same
 * "nothing to show" idiom as `sandboxes.tsx`'s own default-off archived filter.
 */
export function groupThreads(
  threads: ThreadView[],
  sandboxes: Record<string, SandboxView>,
  includeArchived: boolean
): ThreadGroup[] {
  const groups = new Map<string, ThreadGroup>();
  for (const thread of threads) {
    if (!includeArchived && thread.archived) continue;
    let group = groups.get(thread.sandbox);
    if (!group) {
      group = { sandboxName: thread.sandbox, sandbox: sandboxes[thread.sandbox] ?? null, threads: [] };
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
  return thread.harness === "HARNESS_STATE_RUNNING" ? "ok" : "gray";
}
