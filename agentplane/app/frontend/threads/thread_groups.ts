import type { ThreadView } from "./client";
import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";

/**
 * A flat newest-first Thread list folded into Sandbox groups, including Sandboxes without
 * Threads. A missing Sandbox still has a group for its retained Thread history.
 */

export interface ThreadGroup {
  /** Name plus nonempty Sandbox UID, so recreated incarnations remain separate in the list. */
  identity: string;
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
  const identity = (name: string, uid: string | undefined) => JSON.stringify([name, uid || null]);
  for (const thread of threads) {
    const threadId = thread.metadata.threadId;
    const sandbox = thread.summary.sandbox;
    const sandboxName = sandbox?.name ?? "";
    const sandboxUID = sandbox?.uid;
    const key = identity(sandboxName, sandboxUID);
    if (!includeArchived && thread.metadata.archived && threadId !== openThreadId) continue;
    let group = groups.get(key);
    if (!group) {
      const candidate = sandboxes[sandboxName] ?? null;
      group = {
        identity: key,
        sandboxName,
        sandbox: sandboxUID && candidate?.target?.uid === sandboxUID ? candidate : null,
        threads: [],
      };
      groups.set(key, group);
    }
    group.threads.push(thread);
  }
  for (const sandbox of Object.values(sandboxes)) {
    const { name, uid } = sandbox.target!;
    const key = identity(name, uid);
    if (!groups.has(key)) {
      groups.set(key, { identity: key, sandboxName: name, sandbox, threads: [] });
    }
  }
  return [...groups.values()];
}
