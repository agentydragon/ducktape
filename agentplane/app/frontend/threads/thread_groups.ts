import type { ThreadView } from "../schema";
import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";

/**
 * A flat newest-first Thread list folded into Sandbox groups, including Sandboxes without
 * Threads. A missing Sandbox still has a group for its retained Thread history.
 */

export interface ThreadGroup {
  sandboxName: string;
  /** The Sandbox's live view, or null once it no longer exists. */
  sandbox: EnvironmentDescriptor | null;
  /** Newest first, matching the API's own order. */
  threads: ThreadView[];
}

/**
 * Groups with visible Threads come first in newest-Thread order. Existing Sandboxes without a
 * visible Thread follow in inventory order. Archived filtering never hides an existing Sandbox,
 * nor the Thread that is open (`openThreadId`), so the reader can always find where they are.
 */
export function groupThreads(
  threads: ThreadView[],
  sandboxes: Record<string, EnvironmentDescriptor>,
  includeArchived: boolean,
  openThreadId: string | null
): ThreadGroup[] {
  const groups = new Map<string, ThreadGroup>();
  for (const thread of threads) {
    if (!includeArchived && thread.archived && thread.id !== openThreadId) continue;
    let group = groups.get(thread.sandbox);
    if (!group) {
      group = { sandboxName: thread.sandbox, sandbox: sandboxes[thread.sandbox] ?? null, threads: [] };
      groups.set(thread.sandbox, group);
    }
    group.threads.push(thread);
  }
  for (const sandbox of Object.values(sandboxes)) {
    if (!groups.has(sandbox.target!.name)) {
      groups.set(sandbox.target!.name, { sandboxName: sandbox.target!.name, sandbox, threads: [] });
    }
  }
  return [...groups.values()];
}
