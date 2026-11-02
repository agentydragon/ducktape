import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";
import { Checkbox } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import { displayableError } from "../client";
import { setEnvironmentWakeOnSend } from "./client";

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
  sandbox: EnvironmentDescriptor;
  disabled: boolean;
  /** Shown beside the box; the box is named for assistive technology either way. */
  label?: string;
  onError: (message: string | null) => void;
}): JSX.Element {
  const stored = sandbox.configuration?.wakeOnSend ?? false;
  const [requested, setRequested] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (requested === stored) setRequested(null);
  }, [requested, stored]);

  async function save(wakeOnSend: boolean): Promise<void> {
    setRequested(wakeOnSend);
    setSaving(true);
    const failure = await setEnvironmentWakeOnSend(sandbox.target!, wakeOnSend).then(
      () => null,
      (reason: unknown) => reason
    );
    setSaving(false);
    if (failure) setRequested(null);
    onError(failure ? displayableError(failure) : null);
  }

  return (
    <Checkbox
      label={label}
      aria-label={"Wake on message for " + sandbox.target!.name}
      checked={requested ?? stored}
      disabled={disabled || saving}
      onChange={(event) => void save(event.currentTarget.checked)}
    />
  );
}
