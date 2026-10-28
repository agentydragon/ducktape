import { type JSX } from "react";
import { Group } from "@mantine/core";

import type { ThreadView } from "../schema";
import { displayableError } from "../client";
import { renameThread } from "./client";
import { InlineName } from "../inline_name";
import "./thread_title.css";

/** The thread's name. The placeholder is the thread ID; a blank name clears it back to that. */
export function ThreadTitle({
  threadId,
  thread,
  className,
  onRenamed,
  onError,
}: {
  threadId: string;
  thread: ThreadView | null;
  /** The root element's, for the caller to place the title. */
  className?: string;
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
    <Group className={className} gap="xs" wrap="nowrap">
      <InlineName
        className="agentplane-thread-title-name"
        label="Thread name"
        name={thread?.name ?? null}
        placeholder={threadId}
        maxLength={200}
        disabled={thread === null}
        onCommit={(name) => void rename(name)}
      />
    </Group>
  );
}
