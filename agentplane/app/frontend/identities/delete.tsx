/**
 * Identity deletion as the identity list and the identity page offer it. The Policy Controller rechecks Flux
 * ownership and references; the rules are restated here so the UI offers the delete only when it will be accepted.
 */
import { Alert, Button, Group, List, Modal, Stack, Text } from "@mantine/core";
import { type JSX, useState } from "react";

import type { Identity } from "../../../policy/v1/policy_service_pb";
import { deleteIdentity, type DeleteOutcome, type Reference } from "./client";
import { identityOrigin } from "./origin";
import type { IdentityBindings } from "./references";

/** Why delete is unavailable, or null when the Policy Controller would accept it. */
export type DeleteBlocker = { case: "git" } | { case: "referenced"; references: Reference[] };

export function deleteBlocker(identity: Identity, split: IdentityBindings): DeleteBlocker | null {
  if (identityOrigin(identity) === "git") return { case: "git" };
  if (split.referencing.length === 0) return null;
  return {
    case: "referenced",
    references: split.referencing.map((binding) => ({ kind: "PolicyBinding", name: binding.identity?.name ?? "" })),
  };
}

/** One line for a tooltip. */
export function deleteBlockerSummary(blocker: DeleteBlocker): string {
  if (blocker.case === "git") return "Managed in git: remove it in the repository";
  const count = blocker.references.length;
  return `Referenced by ${count} ${count === 1 ? "binding" : "bindings"} it does not own`;
}

export function DeleteBlockerDetail({ blocker }: { blocker: DeleteBlocker }): JSX.Element {
  if (blocker.case === "git")
    return (
      <Text size="sm">Flux applies this identity from git and would recreate it: remove it in the repository.</Text>
    );
  return <References references={blocker.references} />;
}

function References({ references }: { references: Reference[] }): JSX.Element {
  return (
    <Stack gap={4}>
      <Text size="sm">
        A binding naming this identity without being its own would grant a later identity of the same name; a workload
        credential naming it acts as it. Delete these first:
      </Text>
      <List size="sm" aria-label="References to this identity">
        {references.map(({ kind, name }) => (
          <List.Item key={`${kind} ${name}`}>
            {kind} {name}
          </List.Item>
        ))}
      </List>
    </Stack>
  );
}

/** Asks before deleting; a refusal or failure stays in the modal until it is closed. */
export function ConfirmIdentityDelete({
  identity,
  opened,
  onClose,
  onDeleted,
}: {
  identity: Identity;
  opened: boolean;
  onClose: () => void;
  onDeleted: () => void;
}): JSX.Element {
  const [outcome, setOutcome] = useState<DeleteOutcome | null>(null);
  const [pending, setPending] = useState(false);
  const name = identity.identity?.name ?? "";
  function close(): void {
    setOutcome(null);
    onClose();
  }
  async function remove(): Promise<void> {
    setOutcome(null);
    setPending(true);
    const result = await deleteIdentity(identity);
    setPending(false);
    if (result.case === "deleted") {
      close();
      onDeleted();
    } else setOutcome(result);
  }
  return (
    <Modal opened={opened} onClose={close} title={`Delete ${name}?`}>
      <Stack>
        <Text size="sm">
          Its own bindings go with it. A Sandbox still naming it cannot start a workload until it names another.
        </Text>
        {outcome?.case === "referenced" && (
          <Alert color="red" role="alert" title="The Policy Controller refused the delete">
            <References references={outcome.references} />
          </Alert>
        )}
        {outcome?.case === "failed" && (
          <Alert color="red" role="alert" title="Could not delete the identity">
            {outcome.error}
          </Alert>
        )}
        <Group justify="flex-end">
          <Button variant="default" onClick={close}>
            Cancel
          </Button>
          <Button color="red" loading={pending} onClick={() => void remove()}>
            Delete
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
