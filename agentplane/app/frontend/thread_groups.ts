/**
 * A flat newest-first Thread list folded into Sandbox groups, including Sandboxes without
 * Threads. A missing Sandbox still has a group for its retained Thread history.
 */
import type { SandboxKind, SandboxView, ThreadView } from "./client";

export interface ThreadGroup {
  sandboxName: string;
  sandboxKind: SandboxKind;
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
    const sandboxKind = thread.sandbox_kind ?? "agent_sandbox";
    const key = `${sandboxKind}/${thread.sandbox}`;
    let group = groups.get(key);
    if (!group) {
      group = {
        sandboxName: thread.sandbox,
        sandboxKind,
        sandbox: sandboxes[key] ?? null,
        threads: [],
      };
      groups.set(key, group);
    }
    group.threads.push(thread);
  }
  for (const sandbox of Object.values(sandboxes)) {
    const sandboxKind = sandbox.kind ?? "agent_sandbox";
    const key = `${sandboxKind}/${sandbox.name}`;
    if (!groups.has(key)) {
      groups.set(key, { sandboxName: sandbox.name, sandboxKind, sandbox, threads: [] });
    }
  }
  return [...groups.values()];
}

export function archivedCount(threads: ThreadView[]): number {
  return threads.filter((thread) => thread.archived).length;
}
