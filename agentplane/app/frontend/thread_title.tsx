import { Group, Text, TextInput } from "@mantine/core";
import { type JSX, useState } from "react";

import { displayableError, renameThread, type ThreadView } from "./client";
import "./thread_title.css";

/**
 * The thread's title, edited where it is read: the field is the title, styled as one, and a hover
 * is the only hint that it takes typing. Enter commits and Escape puts the stored name back; moving
 * away commits too, so a rename is never lost by clicking elsewhere -- renaming again is one edit,
 * where losing what was typed is not recoverable at all.
 *
 * The placeholder is the thread id; a blank name clears it back to that.
 */
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
  const [draft, setDraft] = useState<string | null>(null);
  // The stored name while nothing is being typed, so a rename that arrives from elsewhere shows.
  const shown = draft ?? thread?.name ?? "";

  async function commit(): Promise<void> {
    if (draft === null || thread === null) return;
    const name = draft.trim() || null;
    setDraft(null);
    if (name === thread.name) return;
    try {
      onRenamed(await renameThread(thread.id, name));
    } catch (reason: unknown) {
      onError(displayableError(reason));
    }
  }

  return (
    // The title and its stable id; on a phone the pair takes a row of its own.
    <Group gap="xs" className="agentplane-thread-name">
      <TextInput
        aria-label="Thread name"
        disabled={thread === null}
        variant="unstyled"
        size="xl"
        value={shown}
        placeholder={threadId}
        maxLength={200}
        classNames={{ input: "agentplane-thread-name-input" }}
        style={{ flex: "1 1 12rem", minWidth: 0 }}
        onChange={(event) => setDraft(event.currentTarget.value)}
        onBlur={() => void commit()}
        onKeyDown={(event) => {
          if (event.key === "Enter") event.currentTarget.blur();
          if (event.key === "Escape") setDraft(null);
        }}
      />
      {thread?.name && (
        <Text size="sm" c="dimmed" style={{ overflowWrap: "anywhere", maxWidth: "100%" }}>
          {threadId}
        </Text>
      )}
    </Group>
  );
}
