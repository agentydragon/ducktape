import { type FormEvent, useState } from "react";

import { sendFeedback } from "./client.ts";

// Submit lifecycle as a closed union so the spinner/disabled/error states can't be
// combined into something nonsensical (e.g. "sending" while also showing an error).
type SubmitState =
  | { status: "idle" }
  | { status: "sending" }
  | { status: "sent" }
  | { status: "error"; message: string };

interface FeedbackFormProps {
  rows: number;
  placeholder: string;
  submitLabel: string;
  // When set, the note is tagged with this item id (per-item feedback); otherwise a
  // global note to Haku. Haku's run contract reads an item-id-referencing note as
  // feedback on that item.
  itemId?: string;
}

// Textarea + submit button that appends an intake note for Haku. Shared by the global
// "Note to Haku" box and the per-item box. While the commit-push is in flight the
// button shows a spinner and the form is disabled, so the operator sees it land.
export function FeedbackForm({ rows, placeholder, submitLabel, itemId }: FeedbackFormProps) {
  const [text, setText] = useState("");
  const [state, setState] = useState<SubmitState>({ status: "idle" });

  function submit(event: FormEvent) {
    event.preventDefault();
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

  const sending = state.status === "sending";
  return (
    <form className={itemId ? "feedback-form item" : "feedback-form"} onSubmit={submit}>
      <textarea
        rows={rows}
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          if (state.status !== "idle") setState({ status: "idle" });
        }}
        placeholder={placeholder}
        disabled={sending}
        required
      />
      <button type="submit" disabled={sending}>
        {sending && <span className="spinner" aria-hidden="true" />}
        {sending ? "Sending…" : state.status === "sent" ? "Sent ✓" : submitLabel}
      </button>
      {state.status === "error" && (
        <span className="feedback-error" role="alert">
          Failed: {state.message}
        </span>
      )}
    </form>
  );
}
