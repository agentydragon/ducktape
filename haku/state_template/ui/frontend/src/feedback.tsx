import { Button, Group, Text, Textarea } from "@mantine/core";
import { type FormEvent, type KeyboardEvent, useState } from "react";

import { sendFeedback } from "./client.ts";

// Submit lifecycle as a closed union so the spinner/disabled states can't be
// combined into something nonsensical.
type SubmitState =
  | { status: "idle" }
  | { status: "sending" }
  | { status: "sent" }
  | { status: "error"; message: string };

interface FeedbackFormProps {
  minRows: number;
  placeholder: string;
  submitLabel: string;
  // When set, the note is tagged with this item id (per-item feedback); otherwise a
  // global note to Haku. Haku reads an item-id-referencing note as feedback on it.
  itemId?: string;
}

// Textarea + submit that appends an intake note for Haku. Enter sends; Shift+Enter
// inserts a newline. Shared by the global "Note to Haku" box and the per-item box.
export function FeedbackForm({ minRows, placeholder, submitLabel, itemId }: FeedbackFormProps) {
  const [text, setText] = useState("");
  const [state, setState] = useState<SubmitState>({ status: "idle" });

  function send() {
    if (!text.trim() || state.status === "sending") return;
    setState({ status: "sending" });
    void sendFeedback(text, itemId)
      .then(() => {
        setText("");
        setState({ status: "sent" });
      })
      .catch((e: unknown) => {
        setState({ status: "error", message: e instanceof Error ? e.message : String(e) });
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
      <Group gap="sm" mt="xs" align="center">
        <Button type="submit" size="xs" loading={sending} disabled={sending || !text.trim()}>
          {state.status === "sent" ? "Sent ✓" : submitLabel}
        </Button>
        <Text size="xs" c="dimmed">
          Shift+Enter for newline
        </Text>
        {state.status === "error" && (
          <Text size="xs" c="red">
            {state.message}
          </Text>
        )}
      </Group>
    </form>
  );
}
