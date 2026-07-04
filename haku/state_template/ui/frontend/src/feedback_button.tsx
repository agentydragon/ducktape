import { Button, Modal } from "@mantine/core";
import { useState } from "react";

import { FeedbackForm } from "./feedback.tsx";
import type { FeedbackContext } from "./types.ts";

// Header "Note to Haku" entry point. A button in the always-visible top bar (so it works
// on mobile no matter how the shell sizes the iframe) that opens the global FeedbackForm
// in a modal — no itemId, so it writes a global intake note. Replaced the bottom-right FAB,
// which `position:fixed` pinned to the iframe's 100dvh box and so hid under the mobile
// browser's bottom toolbar.
export function NoteToHaku() {
  const [open, setOpen] = useState(false);
  const [context, setContext] = useState<FeedbackContext | null>(null);

  // Snapshot which page we're on and any selected text AT CLICK TIME — focusing the modal's
  // textarea clears the selection, so we can't read it later. Page = the URL hash (default "#/").
  function openNote() {
    const selection = window.getSelection()?.toString().trim();
    setContext({ page: window.location.hash || "#/", selection: selection ? selection : null });
    setOpen(true);
  }

  return (
    <>
      <Button variant="default" size="sm" onClick={openNote}>
        💬 Note
      </Button>
      <Modal opened={open} onClose={() => setOpen(false)} title="Note to Haku" size="lg" centered>
        <FeedbackForm
          minRows={4}
          placeholder="Anything for Haku to fold into its next run…"
          submitLabel="Send to Haku"
          context={context ?? undefined}
        />
      </Modal>
    </>
  );
}
