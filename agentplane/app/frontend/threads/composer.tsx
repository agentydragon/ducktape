import { ActionIcon, Button, Group, Textarea } from "@mantine/core";
import { type JSX, type KeyboardEvent, type ReactNode, useEffect, useRef, useState } from "react";

import { IconPlayerStop, IconSend } from "../icons";

export const DEFAULT_COMPOSER_PLACEHOLDER = "Enter sends, Shift+Enter or Ctrl+Enter for a new line";

export interface ThreadComposerProps {
  canSend: boolean;
  running: boolean;
  activeTurn: string | null;
  canResumeSandbox: boolean;
  composerPlaceholder: string;
  onResumeSandbox: () => void;
  onSubmit: (text: string) => boolean;
  onInterrupt: (turnId: string) => void;
  status: ReactNode;
  children: ReactNode;
}

export function ThreadComposer({
  canSend,
  running,
  activeTurn,
  canResumeSandbox,
  composerPlaceholder,
  onResumeSandbox,
  onSubmit,
  onInterrupt,
  status,
  children,
}: ThreadComposerProps): JSX.Element {
  const [draft, setDraft] = useState("");
  // Two Enters before the cleared draft renders would otherwise submit the same text twice, under
  // two command ids. Guards one render, not the lifetime of any HTTP request or command.
  const submitting = useRef(false);
  useEffect(() => {
    submitting.current = false;
  }, [draft]);

  function submit(): void {
    if (!draft.trim() || !canSend || submitting.current) return;
    submitting.current = true;
    if (onSubmit(draft)) setDraft("");
    else submitting.current = false;
  }

  function composerKey(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (!(event.ctrlKey || event.metaKey || event.shiftKey)) {
      submit();
      return;
    }
    // Insert the newline by hand: the preventDefault above already swallowed whatever the browser
    // would otherwise have done for Ctrl/Cmd/Shift+Enter, and setting a controlled value leaves the
    // caret at the end, so put it back where the newline went.
    const field = event.currentTarget;
    const at = field.selectionStart;
    setDraft(`${draft.slice(0, at)}\n${draft.slice(field.selectionEnd)}`);
    requestAnimationFrame(() => field.setSelectionRange(at + 1, at + 1));
  }

  return (
    <>
      <Textarea
        value={draft}
        onChange={(event) => setDraft(event.currentTarget.value)}
        aria-label="Message"
        placeholder={composerPlaceholder}
        autosize
        minRows={
          composerPlaceholder === DEFAULT_COMPOSER_PLACEHOLDER
            ? 2
            : composerPlaceholder.endsWith(DEFAULT_COMPOSER_PLACEHOLDER)
              ? 4
              : 3
        }
        maxRows={12}
        disabled={!canSend}
        onKeyDown={composerKey}
      />
      {status}
      <Group className="agentplane-composer-controls" justify="space-between" gap="xs" wrap="nowrap" pb="xs">
        {children}
        <Group className="agentplane-composer-send" gap="xs" wrap="nowrap">
          {canResumeSandbox ? (
            <Button size="xs" variant="light" onClick={onResumeSandbox}>
              Resume Sandbox
            </Button>
          ) : (
            <>
              <ActionIcon
                size="lg"
                variant="light"
                color="red"
                aria-label="Interrupt"
                disabled={!running || !activeTurn}
                onClick={() => activeTurn && onInterrupt(activeTurn)}
              >
                <IconPlayerStop size={16} />
              </ActionIcon>
              <ActionIcon size="lg" aria-label="Send" disabled={!canSend || !draft.trim()} onClick={submit}>
                <IconSend size={16} />
              </ActionIcon>
            </>
          )}
        </Group>
      </Group>
    </>
  );
}
