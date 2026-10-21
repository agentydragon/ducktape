import type { JSX } from "react";

import { displayableError, renameThread, type ThreadView } from "../client";
import { InlineName } from "../inline_name";

/** The thread's title. The placeholder is the thread id; a blank name clears it back to that. */
export function ThreadTitle({
  threadId,
  thread,
  onRenamed,
  onError,
}: {
  threadId: string;
  thread: ThreadView | null;
  onRenamed: (thread: ThreadView) => void;
  onError: (message: string) => void;
}): JSX.Element {
  async function rename(name: string | null): Promise<void> {
    if (thread === null) return;
    try {
      onRenamed(await renameThread(thread.id, name));
    } catch (reason: unknown) {
      onError(displayableError(reason));
    }
  }

  return (
    <InlineName
      label="Thread name"
      name={thread?.name ?? null}
      placeholder={threadId}
      maxLength={200}
      disabled={thread === null}
      onCommit={(name) => void rename(name)}
    />
  );
}
