import { Alert, Group, Stack, Text, Title } from "@mantine/core";
import type { JSX } from "react";

import { BINDING_GRANT_HELP, BindingsTable } from "../bindings/table";
import { usePolicyResources } from "../policy/watch";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";
import type { Identity } from "../../../proto/identity_pb";

/** Observe the policy resources naming the currently live, bound Identity. */
export function CallerGrants({ identity }: { identity: Identity }): JSX.Element {
  const policy = usePolicyResources({ case: "identity", value: identity.name }, identity.uid);
  return (
    <Stack component="section" aria-label={`Grants for ${identity.name}`} gap="sm" py="sm">
      <Title order={5}>Grants for {identity.name}</Title>
      <Text size="sm" c="dimmed">
        Policy bindings shared by clients and workloads using this Identity. Kubernetes RBAC is not included.
      </Text>
      {policy.error && (
        <Alert color="red" role="alert">
          Could not load grants: {policy.error}
        </Alert>
      )}
      {!policy.resources && !policy.error && <Text role="status">Loading grants…</Text>}
      {policy.resources && (
        <>
          <Group gap={4}>
            <Title order={6}>Bindings</Title>
            <HelpTooltip topic="Bindings" description={BINDING_GRANT_HELP} />
          </Group>
          <BindingsTable
            bindings={policy.resources.bindings}
            policySets={policy.resources.sets}
            onRevoke={null}
            subjectKind="identity"
          />
        </>
      )}
    </Stack>
  );
}
