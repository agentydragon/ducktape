/**
 * The sidebar's grouping: `GET /threads/with-sandboxes`'s flat, newest-first Thread list folded
 * into Sandbox groups, including Sandboxes without Threads. A Thread's `sandbox` name missing from the response's
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
 * Groups with visible Threads come first in newest-Thread order. Existing Sandboxes without a
 * visible Thread follow in inventory order. Archived filtering never hides an existing Sandbox.
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
  for (const sandbox of Object.values(sandboxes)) {
    if (!groups.has(sandbox.name)) {
      groups.set(sandbox.name, { sandboxName: sandbox.name, sandbox, threads: [] });
    }
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
