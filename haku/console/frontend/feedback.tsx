import { type FormEvent, type KeyboardEvent, useState } from "react";
import { Button, Text, Textarea } from "@mantine/core";

import { sendFeedback } from "./client.ts";

// Submit lifecycle as a closed union so the spinner/disabled/error states can't be
// combined into something nonsensical (e.g. "sending" while also showing an error).
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
  // global note to Haku. Haku's run contract reads an item-id-referencing note as
  // feedback on that item.
  itemId?: string;
}

// Textarea + submit button that appends an intake note for Haku. Shared by the global
// "Note to Haku" box and the per-item box. The Mantine Button's `loading` shows a
// spinner and disables the form while the commit-push is in flight, so the operator
// sees it land; a failure surfaces inline on the Textarea instead of being swallowed.
//
// Enter-to-send: the textarea's default (Enter inserts a newline) is inverted so the
// common "type a line, hit Enter" sends without reaching for the mouse — the ↵ on the
// button and the hint by it advertise this. A newline still needs Ctrl/Cmd+Enter (or
// Shift+Enter).
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
    if (event.key !== "Enter") return;
    if (event.ctrlKey || event.metaKey) {
      // Ctrl/Cmd+Enter inserts a newline at the caret (the browser default does not).
      event.preventDefault();
      const ta = event.currentTarget;
      const start = ta.selectionStart ?? ta.value.length;
      const end = ta.selectionEnd ?? ta.value.length;
      ta.setRangeText("\n", start, end, "end");
      setText(ta.value);
      if (state.status !== "idle") setState({ status: "idle" });
    } else if (!event.shiftKey) {
      // Plain Enter sends; Shift+Enter falls through to the default newline.
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
      className="flex w-full flex-col items-start gap-2"
    >
      <Textarea
        className="w-full"
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
        error={state.status === "error" ? `Failed: ${state.message}` : undefined}
      />
      <div className="flex items-center gap-3">
        <Button
          type="submit"
          color="teal"
          loading={sending}
          disabled={!text.trim()}
          rightSection={
            <span aria-hidden="true" className="opacity-70">
              ↵
            </span>
          }
        >
          {state.status === "sent" ? "Sent ✓" : submitLabel}
        </Button>
        <Text size="xs" c="dimmed">
          Ctrl+Enter for newline
        </Text>
      </div>
    </form>
  );
}
