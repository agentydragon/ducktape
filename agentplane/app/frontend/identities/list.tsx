import { ActionIcon, Alert, Button, Group, Stack, Switch, Table, Text, Title, Tooltip } from "@mantine/core";
import { type JSX, useState } from "react";
import { useSearchParams } from "react-router";

import type { Credential } from "../../../egress/api/rules/egress_rules_pb";
import type { Identity, PolicyBinding, PolicySet } from "../../../policy/v1/policy_service_pb";
import { useCredentialMetadata } from "../egress/credential_watch";
import { useIdentities, usePolicyResources } from "../policy/watch";
import { TopbarTitle } from "../shell/topbar";
import { IconTrash } from "../ui/icons";
import { kubernetesAccounts } from "./accounts";
import { ConfirmIdentityDelete, deleteBlocker, deleteBlockerSummary } from "./delete";
import { identityOrigin } from "./origin";
import { identityBindings } from "./references";
import { AccountsReached, BindingExpiries, OriginBadge } from "./summary";

function IdentityRow({
  identity,
  bindings,
  sets,
  credentials,
  onOpen,
}: {
  identity: Identity;
  bindings: readonly PolicyBinding[];
  sets: ReadonlyMap<string, PolicySet>;
  credentials: ReadonlyMap<string, Credential> | null;
  onOpen: (name: string) => void;
}): JSX.Element {
  const name = identity.identity?.name ?? "";
  const split = identityBindings(identity, bindings);
  const naming = [...split.own, ...split.referencing];
  const reached = kubernetesAccounts(naming, sets, credentials);
  const blocker = deleteBlocker(identity, split);
  const [confirming, setConfirming] = useState(false);
  const references =
    split.referencing.length === 0 ? (
      <Text size="sm" c="dimmed">
        No referring bindings
      </Text>
    ) : (
      <Text size="sm">
        {split.referencing.length} {split.referencing.length === 1 ? "binding" : "bindings"} it does not own
      </Text>
    );
  return (
    <Table.Tr>
      <Table.Td>
        <Stack gap={4} align="flex-start">
          <Button variant="subtle" px="xs" onClick={() => onOpen(name)}>
            {name}
          </Button>
          <Text size="sm" px="xs">
            {identity.spec?.description}
          </Text>
          <Group gap="xs" px="xs">
            <OriginBadge origin={identityOrigin(identity)} />
          </Group>
          {/* On a phone the other columns fold under the name. */}
          <Stack gap="xs" px="xs" hiddenFrom="sm">
            <div>
              <Text size="xs" fw={600}>
                Bindings
              </Text>
              <BindingExpiries bindings={naming} />
            </div>
            <div>
              <Text size="xs" fw={600}>
                Kubernetes accounts
              </Text>
              <AccountsReached reached={reached} />
            </div>
            <div>
              <Text size="xs" fw={600}>
                Referenced by
              </Text>
              {references}
            </div>
          </Stack>
        </Stack>
      </Table.Td>
      <Table.Td visibleFrom="sm">
        <BindingExpiries bindings={naming} />
      </Table.Td>
      <Table.Td visibleFrom="sm">
        <AccountsReached reached={reached} />
      </Table.Td>
      <Table.Td visibleFrom="sm">{references}</Table.Td>
      <Table.Td>
        <Tooltip label={blocker ? deleteBlockerSummary(blocker) : `Delete ${name}`} withArrow>
          <ActionIcon
            color="red"
            variant="subtle"
            aria-label={`Delete ${name}`}
            disabled={blocker !== null}
            onClick={() => setConfirming(true)}
          >
            <IconTrash size={16} />
          </ActionIcon>
        </Tooltip>
        {/* The identity watch drops the row once the delete lands. */}
        <ConfirmIdentityDelete
          identity={identity}
          opened={confirming}
          onClose={() => setConfirming(false)}
          onDeleted={() => {}}
        />
      </Table.Td>
    </Table.Tr>
  );
}

/** Every identity with its bindings, the accounts they reach, and what references it. */
export function IdentityList({ onOpen }: { onOpen: (name: string) => void }): JSX.Element {
  const [params, setParams] = useSearchParams();
  const onlyUnreferenced = params.get("unreferenced") === "1";
  const { identities, error: identityError } = useIdentities();
  const policy = usePolicyResources({ case: "everyIdentity", value: true }, undefined);
  const credentials = useCredentialMetadata();
  const error = identityError ?? policy.error;
  const resources = policy.resources;
  const sets = new Map((resources?.sets ?? []).map((set) => [set.identity?.name ?? "", set]));
  const rows =
    identities && resources
      ? [...identities]
          .sort((a, b) => (a.identity?.name ?? "").localeCompare(b.identity?.name ?? ""))
          .filter(
            (identity) => !onlyUnreferenced || identityBindings(identity, resources.bindings).referencing.length === 0
          )
      : null;
  return (
    <Stack>
      <TopbarTitle>
        <Title order={1} size="h4">
          Identities
        </Title>
      </TopbarTitle>
      <Text c="dimmed" size="sm">
        An identity carries grants independently of the sandboxes that use it.
      </Text>
      <Switch
        label="Only identities without referring bindings"
        checked={onlyUnreferenced}
        onChange={(event) => {
          const next = new URLSearchParams(params);
          if (event.currentTarget.checked) next.set("unreferenced", "1");
          else next.delete("unreferenced");
          setParams(next, { replace: true });
        }}
      />
      <Text size="xs" c="dimmed">
        References counted here are bindings the identity does not own. Sandboxes, Sessions and Connections naming an
        identity are not shown yet.
      </Text>
      {error && (
        <Alert color="red" role="alert">
          Could not load identities: {error}
        </Alert>
      )}
      {rows === null && !error && <Text role="status">Loading identities…</Text>}
      {rows && (
        <Table aria-label="Identities">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Identity</Table.Th>
              <Table.Th visibleFrom="sm">Bindings</Table.Th>
              <Table.Th visibleFrom="sm">Kubernetes accounts</Table.Th>
              <Table.Th visibleFrom="sm">Referenced by</Table.Th>
              <Table.Th aria-label="Actions" />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {rows.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={5}>
                  <Text size="sm" c="dimmed">
                    {onlyUnreferenced ? "Every identity has a referring binding." : "No identities."}
                  </Text>
                </Table.Td>
              </Table.Tr>
            )}
            {rows.map((identity) => (
              <IdentityRow
                key={identity.identity?.uid}
                identity={identity}
                bindings={resources?.bindings ?? []}
                sets={sets}
                credentials={credentials}
                onOpen={onOpen}
              />
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}
