import { Anchor, Alert, Button, Group, Stack, Text, Title, Tooltip } from "@mantine/core";
import { type JSX, useState } from "react";

import type { Identity } from "../../../policy/v1/policy_service_pb";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";
import { sandboxInventoryFresh, useRequiredSandboxesLive } from "../sandboxes/live";
import { useCredentialMetadata } from "../egress/credential_watch";
import { BINDING_GRANT_HELP, BindingsTable } from "../policy/table";
import { IconArrowLeft } from "../ui/icons";
import { useIdentities, usePolicyResources } from "../policy/watch";
import { TopbarTitle } from "../shell/topbar";
import { kubernetesAccounts } from "./accounts";
import { ConfirmIdentityDelete, DeleteBlockerDetail, deleteBlocker } from "./delete";
import { identityOrigin } from "./origin";
import { identityBindings, type IdentityBindings } from "./references";
import { AccountsReached, OriginBadge } from "./summary";

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
  const name = identity.identity?.name ?? "";
  const blocker = deleteBlocker(identity, split);
  return (
    <Stack component="section" aria-label="Delete identity" gap="xs">
      <Title order={5}>Delete</Title>
      {blocker ? (
        <Alert color="gray" title="This identity cannot be deleted here">
          <DeleteBlockerDetail blocker={blocker} />
        </Alert>
      ) : (
        <Text size="sm" c="dimmed">
          No external policy binding references this identity. Deleting it also deletes what it owns: {split.own.length}{" "}
          {split.own.length === 1 ? "binding" : "bindings"}.
        </Text>
      )}
      <Group>
        <Tooltip label={blocker ? "Unavailable: see above" : `Delete ${name}`} withArrow>
          <Button color="red" variant="light" disabled={blocker !== null} onClick={() => setConfirming(true)}>
            Delete identity
          </Button>
        </Tooltip>
      </Group>
      <ConfirmIdentityDelete
        identity={identity}
        opened={confirming}
        onClose={() => setConfirming(false)}
        onDeleted={onDeleted}
      />
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
  const inventory = useRequiredSandboxesLive();
  const sandboxes = inventory.snapshot?.filter((box) => box.identityName === name);
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
          <Stack component="section" aria-label="Sandboxes using this identity" gap={4}>
            <Title order={5}>Sandboxes using this identity</Title>
            {!sandboxInventoryFresh(inventory) && (
              <Text size="sm" c="dimmed">
                Waiting for current sandbox inventory…
              </Text>
            )}
            {sandboxInventoryFresh(inventory) && sandboxes?.length === 0 && <Text size="sm">None</Text>}
            {sandboxes?.map(
              ({ target }) =>
                target && (
                  <Anchor size="sm" key={target.uid} href={`/#/sandboxes/${encodeURIComponent(target.name)}`}>
                    {target.name}
                  </Anchor>
                )
            )}
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
