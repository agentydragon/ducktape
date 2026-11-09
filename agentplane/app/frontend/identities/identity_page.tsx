import { Alert, Button, Group, List, Modal, Stack, Text, Title, Tooltip } from "@mantine/core";
import { type JSX, useState } from "react";

import type { Identity } from "../../../policy/v1/policy_service_pb";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";
import { useCredentialMetadata } from "../bindings/credential_watch";
import { BINDING_GRANT_HELP, BindingsTable } from "../bindings/table";
import { IconArrowLeft } from "../icons";
import { useIdentities, usePolicyResources } from "../policy/watch";
import { TopbarTitle } from "../topbar";
import { kubernetesAccounts } from "./accounts";
import { deleteIdentity, type DeleteOutcome, type Reference } from "./client";
import { identityOrigin } from "./origin";
import { identityBindings, type IdentityBindings } from "./references";
import { AccountsReached, OriginBadge } from "./summary";

/** Why delete is unavailable, or null when the Policy Controller would accept it. */
function deleteBlocker(identity: Identity, split: IdentityBindings): JSX.Element | null {
  if (identityOrigin(identity) === "git")
    return (
      <Text size="sm">Flux applies this identity from git and would recreate it: remove it in the repository.</Text>
    );
  if (split.referencing.length === 0) return null;
  return (
    <References
      references={split.referencing.map((binding) => ({ kind: "PolicyBinding", name: binding.identity?.name ?? "" }))}
    />
  );
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

function DeleteIdentity({
  identity,
  split,
  onDeleted,
}: {
  identity: Identity;
  split: IdentityBindings;
  onDeleted: () => void;
}): JSX.Element {
  const [confirming, setConfirming] = useState(false);
  const [outcome, setOutcome] = useState<DeleteOutcome | null>(null);
  const [pending, setPending] = useState(false);
  const name = identity.identity?.name ?? "";
  const blocker = deleteBlocker(identity, split);
  async function remove(): Promise<void> {
    setConfirming(false);
    setPending(true);
    const result = await deleteIdentity(identity);
    setPending(false);
    if (result.case === "deleted") onDeleted();
    else setOutcome(result);
  }
  return (
    <Stack component="section" aria-label="Delete identity" gap="xs">
      <Title order={5}>Delete</Title>
      {blocker ? (
        <Alert color="gray" title="This identity cannot be deleted here">
          {blocker}
        </Alert>
      ) : (
        <Text size="sm" c="dimmed">
          Nothing references this identity. Deleting it also deletes what it owns: {split.own.length}{" "}
          {split.own.length === 1 ? "binding" : "bindings"}.
        </Text>
      )}
      <Group>
        <Tooltip label={blocker ? "Unavailable: see above" : `Delete ${name}`} withArrow>
          <Button
            color="red"
            variant="light"
            disabled={blocker !== null}
            loading={pending}
            onClick={() => setConfirming(true)}
          >
            Delete identity
          </Button>
        </Tooltip>
      </Group>
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
      <Modal opened={confirming} onClose={() => setConfirming(false)} title={`Delete ${name}?`}>
        <Stack>
          <Text size="sm">
            Its own bindings go with it. A Sandbox still naming it cannot start a workload until it names another.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button color="red" onClick={() => void remove()}>
              Delete
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}

/** One identity: what it is, the Kubernetes accounts it reaches, its grants, and its delete. */
export function IdentityPage({
  name,
  onBack,
  onDeleted,
}: {
  name: string;
  onBack: () => void;
  onDeleted: () => void;
}): JSX.Element {
  const { identities, error: identityError } = useIdentities();
  const identity = identities?.find((candidate) => candidate.identity?.name === name) ?? null;
  // The UID fences the bindings view: a same-name replacement starts a fresh watch.
  const policy = usePolicyResources({ case: "identity", value: name }, identity?.identity?.uid);
  const credentials = useCredentialMetadata();
  const error = identityError ?? policy.error;
  const resources = policy.resources;
  const split = identity && resources ? identityBindings(identity, resources.bindings) : null;
  const sets = new Map((resources?.sets ?? []).map((set) => [set.identity?.name ?? "", set]));
  return (
    <Stack>
      <TopbarTitle>
        <Group gap="xs" wrap="nowrap">
          <Button variant="subtle" px={4} aria-label="All identities" onClick={onBack}>
            <IconArrowLeft size={16} />
          </Button>
          <Title order={1} size="h4">
            {name}
          </Title>
        </Group>
      </TopbarTitle>
      {error && (
        <Alert color="red" role="alert">
          Could not load the identity: {error}
        </Alert>
      )}
      {identities && !identity && <Text>No identity named {name}.</Text>}
      {(!identities || (identity && !resources)) && !error && <Text role="status">Loading identity…</Text>}
      {identity && resources && split && (
        <>
          <Stack gap={4}>
            <Text>{identity.spec?.description}</Text>
            <Group gap="xs">
              <OriginBadge origin={identityOrigin(identity)} />
            </Group>
          </Stack>
          <Stack component="section" aria-label="Kubernetes accounts" gap="xs">
            <Title order={5}>Kubernetes accounts</Title>
            <Text size="sm" c="dimmed">
              The ServiceAccounts this identity&apos;s bindings let it act as on the API server.
            </Text>
            <AccountsReached reached={kubernetesAccounts(resources.bindings, sets, credentials)} />
          </Stack>
          <Stack component="section" aria-label={`Grants for ${name}`} gap="xs">
            <Group gap={4}>
              <Title order={5}>Grants</Title>
              <HelpTooltip topic="Bindings" description={BINDING_GRANT_HELP} />
            </Group>
            <Text size="sm" c="dimmed">
              Policy bindings naming this identity. The Action Service&apos;s effective policy is not shown here: it is
              looked up by ServiceAccount, not by identity.
            </Text>
            <BindingsTable
              bindings={resources.bindings}
              policySets={resources.sets}
              onRevoke={null}
              subjectKind="identity"
            />
          </Stack>
          <DeleteIdentity key={identity.identity?.uid} identity={identity} split={split} onDeleted={onDeleted} />
        </>
      )}
    </Stack>
  );
}
