import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";
import { NumberInput } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import { sandboxIdleSuspendAfterSeconds } from "../sandbox_status";
import { displayableError } from "../client";
import { setEnvironmentIdleSuspend } from "./client";

const SECONDS_PER_MINUTE = 60;

/** What the minutes input holds for a stored time: empty means never. */
export function idleMinutes(seconds: number | null | undefined): number | string {
  return seconds === null || seconds === undefined ? "" : seconds / SECONDS_PER_MINUTE;
}

/** The seconds an edited minutes input stands for; null for empty. */
export function idleSeconds(minutes: number | string): number | null {
  return typeof minutes === "number" ? Math.round(minutes * SECONDS_PER_MINUTE) : null;
}

/** The minutes input of the Sandbox creation form; the API refuses under one minute, so the input does too. */
export function IdleSuspendField({
  seconds,
  onChange,
}: {
  seconds: number | null | undefined;
  onChange: (seconds: number | null) => void;
}): JSX.Element {
  return (
    <NumberInput
      label="Suspend when idle (min)"
      placeholder="Never"
      min={1}
      allowNegative={false}
      w="12rem"
      value={idleMinutes(seconds)}
      onChange={(minutes) => onChange(idleSeconds(minutes))}
    />
  );
}

/** A row's idle suspension time, saved on blur or Enter to the incarnation shown; the live list brings the stored value back. */
export function IdleSuspendInput({
  sandbox,
  disabled,
  onError,
}: {
  sandbox: EnvironmentDescriptor;
  disabled: boolean;
  onError: (message: string | null) => void;
}): JSX.Element {
  const stored = sandboxIdleSuspendAfterSeconds(sandbox);
  const [draft, setDraft] = useState<number | string>(idleMinutes(stored));
  const [saving, setSaving] = useState(false);
  useEffect(() => setDraft(idleMinutes(stored)), [stored]);

  async function save(): Promise<void> {
    if (draft === idleMinutes(stored)) return;
    setSaving(true);
    const idleSuspendAfterSeconds = idleSeconds(draft);
    const failure = await setEnvironmentIdleSuspend(sandbox.target!, idleSuspendAfterSeconds).then(
      () => null,
      (reason: unknown) => reason
    );
    setSaving(false);
    if (failure) setDraft(idleMinutes(stored));
    onError(failure ? displayableError(failure) : null);
  }

  return (
    <NumberInput
      aria-label={"Idle suspend minutes for " + sandbox.target!.name}
      placeholder="Never"
      min={1}
      allowNegative={false}
      hideControls
      w="6rem"
      value={draft}
      disabled={disabled || saving}
      onChange={setDraft}
      onBlur={() => void save()}
      onKeyDown={(event) => {
        if (event.key === "Enter") void save();
      }}
    />
  );
}
