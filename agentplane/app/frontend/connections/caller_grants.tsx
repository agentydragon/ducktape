import { Alert, Group, Stack, Text, Title } from "@mantine/core";
import type { JSX } from "react";

import { ActionPolicyWatch } from "../actions/policy";
import { BINDING_GRANT_HELP, BindingsTable } from "../bindings/table";
import { usePolicyResources } from "../policy/watch";
import { serviceAccountKey } from "./service_accounts";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";
import type { ServiceAccount } from "../../../proto/identity_pb";

/** Observe the policy resources naming this ServiceAccount. */
export function CallerGrants({ account }: { account: ServiceAccount }): JSX.Element {
  const policy = usePolicyResources({ case: "subject", value: account }, undefined);
  return (
    <Stack component="section" aria-label={`Grants for ${serviceAccountKey(account)}`} gap="sm" py="sm">
      <Title order={5}>Grants for {serviceAccountKey(account)}</Title>
      <Text size="sm" c="dimmed">
        Policy bindings shared by all clients and workloads using this ServiceAccount. Kubernetes RBAC is not included.
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
            subjectKind="ServiceAccount"
          />
        </>
      )}
      <ActionPolicyWatch subject={account} />
    </Stack>
  );
}
