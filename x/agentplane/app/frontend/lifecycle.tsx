/**
 * The lifecycle controls the sandbox list and the sandbox page share: the suspend/resume button,
 * and deletion, which the API takes only from a suspended sandbox (`inventory.py`). The rule is
 * restated here so the UI offers the action when it will be accepted, not so it enforces anything.
 */
import { ActionIcon, Button, Group, Modal, Stack, Text, Tooltip } from "@mantine/core";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconPlayerPause from "@tabler/icons-react/dist/esm/icons/IconPlayerPause.mjs";
import IconPlayerPlay from "@tabler/icons-react/dist/esm/icons/IconPlayerPlay.mjs";
import IconTrash from "@tabler/icons-react/dist/esm/icons/IconTrash.mjs";

import type { SandboxView } from "./client";

export const DELETE_NEEDS_SUSPENDED = "Suspend the sandbox before deleting it";

/** Whether the API will accept a deletion: an archived sandbox is suspended too, so it may go. */
export function deletable(sandbox: SandboxView): boolean {
  return sandbox.operating_mode === "Suspended";
}

export function SuspendResume({
  sandbox,
  onAct,
}: {
  sandbox: SandboxView;
  onAct: (action: "suspend" | "resume") => void;
}): JSX.Element {
  const resume = sandbox.operating_mode === "Suspended";
  return (
    <Tooltip label={resume ? "Resume" : "Suspend"} withArrow>
      <ActionIcon
        variant="light"
        aria-label={resume ? "Resume" : "Suspend"}
        onClick={() => onAct(resume ? "resume" : "suspend")}
      >
        {resume ? <IconPlayerPlay size={16} /> : <IconPlayerPause size={16} />}
      </ActionIcon>
    </Tooltip>
  );
}

export function DeleteButton({ sandbox, onDelete }: { sandbox: SandboxView; onDelete: () => void }): JSX.Element {
  const allowed = deletable(sandbox);
  return (
    <Tooltip label={allowed ? "Delete" : DELETE_NEEDS_SUSPENDED} withArrow>
      {/* Mantine's `disabled` takes the button out of the pointer events a tooltip needs, and the
          reason is the point here, so the button stays live and refuses instead. */}
      <ActionIcon
        variant="light"
        color="red"
        aria-label="Delete"
        aria-disabled={!allowed}
        data-disabled={allowed ? undefined : true}
        onClick={() => allowed && onDelete()}
      >
        <IconTrash size={16} />
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
