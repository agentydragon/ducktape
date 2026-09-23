/**
 * A flat newest-first Thread list folded into Sandbox groups, including Sandboxes without
 * Threads. A missing Sandbox still has a group for its retained Thread history.
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
