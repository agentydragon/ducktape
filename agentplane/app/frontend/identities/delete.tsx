/**
 * Identity deletion as the identity list and the identity page offer it. The dialog asks the Policy Controller
 * whether it would accept (a dry-run DeleteIdentity) instead of restating its rules: the browser cannot see
 * WorkloadCredentials, one of the references the controller refuses on.
 */
import { Alert, Button, Group, List, Modal, Stack, Text } from "@mantine/core";
import { type JSX, useEffect, useRef, useState } from "react";

import type { Identity } from "../../../policy/v1/policy_service_pb";
import {
  checkIdentityDelete,
  type DeleteCheck,
  deleteIdentity,
  type DeleteOutcome,
  type DeleteRefusal,
  type Reference,
} from "./client";

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

function Refusal({ refusal }: { refusal: DeleteRefusal }): JSX.Element {
  return refusal.case === "referenced" ? (
    <Alert color="red" role="alert" title="The Policy Controller refuses this delete">
      <References references={refusal.references} />
    </Alert>
  ) : (
    <Alert color="red" role="alert" title="This identity cannot be deleted">
      {refusal.error}
    </Alert>
  );
}

/**
 * Asks before deleting. Each time it opens it dry-runs the delete and offers Delete only if the controller would
 * accept; the real delete still reports a refusal for a reference created in between.
 */
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
  const [check, setCheck] = useState<DeleteCheck | null>(null);
  const [outcome, setOutcome] = useState<DeleteOutcome | null>(null);
  const [pending, setPending] = useState(false);
  const name = identity.identity?.name ?? "";
  const uid = identity.identity?.uid;
  // Re-check per opening and per incarnation, not on every watch update of the same Identity.
  const latest = useRef(identity);
  latest.current = identity;
  useEffect(() => {
    if (!opened) return;
    let active = true;
    setCheck(null);
    setOutcome(null);
    void checkIdentityDelete(latest.current).then((result) => {
      if (active) setCheck(result);
    });
    return () => {
      active = false;
    };
  }, [opened, uid]);
  async function remove(): Promise<void> {
    setPending(true);
    const result = await deleteIdentity(identity);
    setPending(false);
    if (result.case === "deleted") {
      onClose();
      onDeleted();
    } else setOutcome(result);
  }
  const refusal: DeleteRefusal | null =
    outcome?.case === "deleted" ? null : (outcome ?? (check?.case === "allowed" ? null : check));
  return (
    <Modal opened={opened} onClose={onClose} title={`Delete ${name}?`}>
      <Stack>
        <Text size="sm">
          Its own bindings go with it. A Sandbox still naming it cannot start a workload until it names another.
        </Text>
        {check === null && (
          <Text size="sm" c="dimmed" role="status">
            Checking with the Policy Controller…
          </Text>
        )}
        {refusal && <Refusal refusal={refusal} />}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            color="red"
            loading={pending}
            disabled={check?.case !== "allowed" || outcome !== null}
            onClick={() => void remove()}
          >
            Delete
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
