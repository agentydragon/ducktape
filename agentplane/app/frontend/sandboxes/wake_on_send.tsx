import { Checkbox } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import type { SandboxView } from "../api/schema";
import { api, displayableError } from "../client";

/**
 * Whether a message to a suspended Sandbox resumes it, saved on each change to the incarnation shown. What
 * was asked for shows until the live list brings the stored value back.
 */
export function WakeOnSendCheckbox({
  sandbox,
  disabled,
  label,
  onError,
}: {
  sandbox: SandboxView;
  disabled: boolean;
  /** Shown beside the box; the box is named for assistive technology either way. */
  label?: string;
  onError: (message: string | null) => void;
}): JSX.Element {
  const stored = sandbox.wake_on_send ?? false;
  const [requested, setRequested] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (requested === stored) setRequested(null);
  }, [requested, stored]);

  async function save(wakeOnSend: boolean): Promise<void> {
    setRequested(wakeOnSend);
    setSaving(true);
    const failure = await api
      .PATCH("/sandboxes/{name}/wake-policy", {
        params: { path: { name: sandbox.name } },
        body: { uid: sandbox.uid, wake_on_send: wakeOnSend },
      })
      .then(
        ({ error }) => error,
        (reason: unknown) => reason
      );
    setSaving(false);
    if (failure) setRequested(null);
    onError(failure ? displayableError(failure) : null);
  }

  return (
    <Checkbox
      label={label}
      aria-label={"Wake on message for " + sandbox.name}
      checked={requested ?? stored}
      disabled={disabled || saving}
      onChange={(event) => void save(event.currentTarget.checked)}
    />
  );
}
