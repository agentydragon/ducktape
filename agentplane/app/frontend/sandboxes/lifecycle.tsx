import type { JSX } from "react";
/**
 * The lifecycle controls the sandbox list and the sandbox page share: the suspend/resume button,
 * and deletion, which the API takes only from a suspended sandbox (`inventory.py`). The rule is
 * restated here so the UI offers the action when it will be accepted, not so it enforces anything.
 */
import { ActionIcon, Button, Group, Modal, Stack, Text, Tooltip } from "@mantine/core";
import type { SandboxView } from "../api/schema";
import { IconPlayerPause, IconPlayerPlay } from "../icons";

export const DELETE_NEEDS_SUSPENDED = "Suspend the sandbox before deleting it";

/** Whether the API will accept a deletion: only a suspended sandbox may go. */
export function deletable(sandbox: SandboxView): boolean {
  return sandbox.phase === "Suspended";
}

/** Deletion was asked for: it takes no new work and cannot be suspended or resumed. */
export function deleting(sandbox: SandboxView): boolean {
  return sandbox.phase === "Quiescing" || sandbox.phase === "Tearing down";
}

export function SuspendResume({
  sandbox,
  onAct,
}: {
  sandbox: SandboxView;
  onAct: (action: "suspend" | "resume") => void;
}): JSX.Element {
  const resume = sandbox.operating_mode === "Suspended";
  const modeUnknown = sandbox.operating_mode === null || sandbox.operating_mode === undefined;
  const disabled = deleting(sandbox) || modeUnknown;
  const label = deleting(sandbox)
    ? "Sandbox deleting"
    : modeUnknown
      ? "Waiting for controller status"
      : resume
        ? "Resume"
        : "Suspend";
  return (
    <Tooltip label={label} withArrow>
      <ActionIcon
        variant="light"
        aria-label={modeUnknown ? "Sandbox mode unavailable" : resume ? "Resume" : "Suspend"}
        disabled={disabled}
        onClick={() => onAct(resume ? "resume" : "suspend")}
      >
        {resume ? <IconPlayerPlay size={16} /> : <IconPlayerPause size={16} />}
      </ActionIcon>
    </Tooltip>
  );
}

/** What deleting asks before it happens; rendered only while the question stands. */
export function ConfirmDelete({
  name,
  onCancel,
  onConfirm,
}: {
  name: string;
  onCancel: () => void;
  onConfirm: () => void;
}): JSX.Element {
  return (
    <Modal opened onClose={onCancel} title={`Delete ${name}?`}>
      <Stack>
        <Text size="sm">
          The Pod and its volume go with the sandbox, and everything written on it. Threads already recorded outlive it.
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onCancel}>
            Cancel
          </Button>
          <Button color="red" onClick={onConfirm}>
            Delete
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
