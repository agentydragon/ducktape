import { Button, Group, Text, Textarea } from "@mantine/core";
import { type FormEvent, type KeyboardEvent, useState } from "react";

import { sendFeedback } from "./client.ts";
import { notifyError } from "./errors.ts";
import type { FeedbackContext } from "./types.ts";

// Submit lifecycle as a closed union so the spinner/disabled states can't be
// combined into something nonsensical. Failure surfaces as a toast (notifyError), not a fourth
// state — the box returns to idle so the operator can retry with their text intact.
type SubmitState = { status: "idle" } | { status: "sending" } | { status: "sent" };

interface FeedbackFormProps {
  minRows: number;
  placeholder: string;
  submitLabel: string;
  // When set, the note is tagged with this item id (per-item feedback); otherwise a
  // global note to Haku. Haku reads an item-id-referencing note as feedback on it.
  itemId?: string;
  // Page + selected-text snapshot to send alongside the note (captured by the caller at the
  // moment the box was opened, before focus clears the selection). Shown to the operator so the
  // captured context is transparent.
  context?: FeedbackContext;
}

// Textarea + submit that appends an intake note for Haku. Enter sends; Shift+Enter
// inserts a newline. Shared by the global "Note to Haku" box and the per-item box.
export function FeedbackForm({ minRows, placeholder, submitLabel, itemId, context }: FeedbackFormProps) {
  const [text, setText] = useState("");
  const [state, setState] = useState<SubmitState>({ status: "idle" });

  function send() {
    if (!text.trim() || state.status === "sending") return;
    setState({ status: "sending" });
    void sendFeedback(text, itemId, context)
      .then(() => {
        setText("");
        setState({ status: "sent" });
      })
      .catch((e: unknown) => {
        notifyError("Couldn't send your note", e);
        setState({ status: "idle" });
      });
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  }

  const sending = state.status === "sending";
  return (
    <form
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        send();
      }}
      style={{ width: "100%" }}
    >
      <Textarea
        autosize
        minRows={minRows}
        value={text}
        onChange={(event) => {
          setText(event.currentTarget.value);
          if (state.status !== "idle") setState({ status: "idle" });
        }}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        disabled={sending}
      />
      {context && (
        <Text size="xs" c="dimmed" mt={4}>
          Reporting from {context.page}
          {context.selection && ` · ${context.selection.length} chars selected`}
        </Text>
      )}
      <Group gap="sm" mt="xs" align="center">
        <Button type="submit" size="xs" loading={sending} disabled={sending || !text.trim()}>
          {state.status === "sent" ? "Sent ✓" : submitLabel}
        </Button>
        <Text size="xs" c="dimmed">
          Shift+Enter for newline
        </Text>
      </Group>
    </form>
  );
}
