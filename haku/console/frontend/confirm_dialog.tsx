import { useEffect, useRef, useState } from "react";
import { Button, Group, Text } from "@mantine/core";

import { ACTION_COLOR } from "./theme.ts";

export interface ConfirmRequest {
  title: string;
  body?: string; // what the action does, in trusted shell copy
  url?: string; // for openLink — the full URL shown verbatim
  approveLabel: string;
}

// Top-layer modal confirm — the only trustworthy surface at the moment of a privileged
// action. Rendered with the native `<dialog>.showModal()` (the browser **top layer**)
// so the cross-origin iframe cannot draw over it, read it, or intercept its clicks; the
// `::backdrop` dims the agent UI so "the shell is talking now" is unambiguous. The
// approve button stays disabled for a beat so a baited click-through can't land on a
// freshly-rendered confirm. See plans/free_form_ui_iframe.md → "The shell as a thin
// trusted layer".
const ARM_DELAY_MS = 400;

export function ConfirmDialog({
  request,
  onApprove,
  onCancel,
}: {
  request: ConfirmRequest | null;
  onApprove: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (request && !d.open) {
      setArmed(false);
      d.showModal();
      const t = setTimeout(() => setArmed(true), ARM_DELAY_MS);
      return () => clearTimeout(t);
    }
    if (!request && d.open) d.close();
  }, [request]);

  return (
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault(); // route Esc through our handler so the iframe gets a result
        onCancel();
      }}
      className="haku-confirm-dialog max-w-md rounded-lg p-5 shadow-xl"
    >
      {request && (
        <div className="flex flex-col gap-3">
          <Text fw={600}>{request.title}</Text>
          {request.body && <Text size="sm">{request.body}</Text>}
          {request.url && (
            <Text size="sm" className="haku-url-preview rounded p-2 font-mono break-all">
              {request.url}
            </Text>
          )}
          <Group justify="flex-end" gap="sm" mt="xs">
            <Button variant="default" size="xs" onClick={onCancel}>
              Cancel
            </Button>
            <Button color={ACTION_COLOR} size="xs" disabled={!armed} onClick={onApprove}>
              {request.approveLabel}
            </Button>
          </Group>
        </div>
      )}
    </dialog>
  );
}
