import { type FormEvent, type KeyboardEvent, useState } from "react";
import { ActionIcon, Button, Popover, Text, Textarea } from "@mantine/core";
// Deep per-icon import (default export), not the barrel: the @tabler barrel makes
// esbuild OOM tree-shaking it (~8.7 GB peak with the full node_modules tree), and
// there's no clean per-action RAM lever (see debug/esbuild_tabler_memory.md). The
// subpath ships no .d.mts, so it's typed via tabler-icons.d.ts.
import IconMessage2 from "@tabler/icons-react/dist/esm/icons/IconMessage2.mjs";

import { postTrace } from "./client.ts";
import { ACTION_COLOR } from "./theme.ts";
import { toastError } from "./toast.ts";

// Submit lifecycle as a closed union so the spinner/disabled states can't be combined
// into something nonsensical. Failures surface as a toast (toast.ts), not inline.
type SubmitState = { status: "idle" } | { status: "sending" } | { status: "sent" };

interface FeedbackFormProps {
  minRows: number;
  placeholder: string;
  submitLabel: string;
}

// Textarea + submit button that appends an intake note for Haku via POST /api/trace.
// The Mantine Button's `loading` shows a spinner and disables the form while the
// commit-push is in flight, so the operator sees it land; a failure surfaces as a
// toast (toast.ts) instead of being swallowed.
//
// Enter-to-send: the textarea's default (Enter inserts a newline) is inverted so the
// common "type a line, hit Enter" sends without reaching for the mouse — the ↵ on the
// button and the hint by it advertise this. A newline still needs Ctrl/Cmd+Enter (or
// Shift+Enter).
export function FeedbackForm({ minRows, placeholder, submitLabel }: FeedbackFormProps) {
  const [text, setText] = useState("");
  const [state, setState] = useState<SubmitState>({ status: "idle" });

  function send() {
    if (!text.trim() || state.status === "sending") return;
    setState({ status: "sending" });
    void postTrace(text)
      .then(() => {
        setText("");
        setState({ status: "sent" });
      })
      .catch((e: unknown) => {
        setState({ status: "idle" });
        toastError("Trace failed", e);
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
      />
      <div className="flex items-center gap-3">
        <Button
          type="submit"
          color={ACTION_COLOR}
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

// Floating corner button that pops the note-to-haku form open as a popover, so the
// form is an on-demand affordance instead of permanent real estate at the top of the
// page. The form itself (FeedbackForm above) is unchanged; this only owns open/close
// state and frames it. The popover closes on outside-click / Esc (Mantine defaults);
// a successful send leaves it open showing "Sent ✓" rather than auto-closing, since
// closing would hide the only success signal (there is no success toast).
export function FeedbackFab() {
  const [open, setOpen] = useState(false);
  return (
    // Fixed to the viewport (not the centered content column) so the button stays
    // pinned bottom-right regardless of scroll. A plain div carries the fixed
    // positioning so no Mantine component styles can override it; the Popover's
    // dropdown is portaled to <body> and still anchors to the ActionIcon.
    <div className="fixed bottom-6 right-6 z-50">
      <Popover opened={open} onChange={setOpen} position="top-end" withArrow shadow="md" width={360}>
        <Popover.Target>
          <ActionIcon color={ACTION_COLOR} variant="filled" size="xl" radius="xl" aria-label="Note to Haku">
            <IconMessage2 size={20} />
          </ActionIcon>
        </Popover.Target>
        <Popover.Dropdown>
          <FeedbackForm
            minRows={4}
            placeholder="Anything for Haku to fold into its next run…"
            submitLabel="Send to Haku"
          />
        </Popover.Dropdown>
      </Popover>
    </div>
  );
}
