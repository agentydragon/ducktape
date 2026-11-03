import { Alert, Group, Stack, Text, Title } from "@mantine/core";
import type { JSX } from "react";

import { ActionPolicyWatch } from "../actions/policy";
import { BINDING_GRANT_HELP, BindingsTable } from "../bindings/table";
import { useFrontendConfig } from "../frontend_config";
import { usePolicyResources } from "../policy/watch";
import { type CallerServiceAccount, serviceAccountKey } from "./models";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";

/** Observe the policy resources naming this ServiceAccount. */
export function CallerGrants({ account }: { account: CallerServiceAccount }): JSX.Element {
  const config = useFrontendConfig();
  const policy = usePolicyResources(config?.policy_namespace, account, undefined);
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
          <BindingsTable bindings={policy.resources.bindings} policySets={policy.resources.sets} onRevoke={null} />
        </>
      )}
      <ActionPolicyWatch subject={account} />
    </Stack>
  );
}
