import { type JSX, useEffect, useEffectEvent, useState } from "react";
import { ActionIcon, Group, Tooltip } from "@mantine/core";
import { IconSparkles } from "../icons";

import { api, autoTitleThread, displayableError, renameThread, type ThreadView } from "../client";
import { InlineName } from "../inline_name";
import "./thread_title.css";

/** The thread's title. The placeholder is the thread id; a blank name clears it back to that. */
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
  const [generating, setGenerating] = useState(false);
  const [titleGenerationAvailable, setTitleGenerationAvailable] = useState(false);
  const reportError = useEffectEvent(onError);

  useEffect(() => {
    let cancelled = false;
    void api
      .GET("/threads/form")
      .then(({ data }) => {
        if (!cancelled && data) setTitleGenerationAvailable(data.title_generation_available);
      })
      .catch((reason: unknown) => {
        if (!cancelled) reportError(displayableError(reason));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function rename(name: string | null): Promise<void> {
    if (thread === null) return;
    try {
      onRenamed(await renameThread(thread.id, name));
    } catch (reason: unknown) {
      onError(displayableError(reason));
    }
  }

  async function autoTitle(): Promise<void> {
    if (thread === null || generating) return;
    setGenerating(true);
    try {
      onRenamed(await autoTitleThread(thread.id));
    } catch (reason: unknown) {
      onError(displayableError(reason));
    } finally {
      setGenerating(false);
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
      {titleGenerationAvailable && (
        <Tooltip label="Auto-title" withArrow>
          <ActionIcon
            size="sm"
            variant="subtle"
            aria-label="Auto-title"
            disabled={thread === null || generating}
            loading={generating}
            onClick={() => void autoTitle()}
          >
            <IconSparkles size={16} />
          </ActionIcon>
        </Tooltip>
      )}
    </Group>
  );
}
