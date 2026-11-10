import { Alert, Stack, Text, Title } from "@mantine/core";
import type { JSX } from "react";

import { BindingsTable } from "../bindings/table";
import { useIdentities, usePolicyResources } from "../policy/watch";

/** Read-only identity and grants attached to the selected Sandbox. */
export function EnvironmentIdentity({
  identityName,
}: {
  /** Undefined while the environment is unavailable, null when its descriptor says it has no identity. */
  identityName: string | null | undefined;
}): JSX.Element {
  if (identityName === undefined) return <Text role="status">Loading environment identity…</Text>;
  if (identityName === null) {
    return (
      <Stack component="section" aria-label="Environment identity" gap="xs">
        <Title order={5}>Identity</Title>
        <Text size="sm" c="dimmed">
          No identity is assigned to this environment.
        </Text>
      </Stack>
    );
  }
  return <NamedEnvironmentIdentity identityName={identityName} />;
}

function NamedEnvironmentIdentity({ identityName }: { identityName: string }): JSX.Element {
  const identityState = useIdentities();
  const identity = identityState.identities?.find((item) => item.identity?.name === identityName);
  const policyState = usePolicyResources(
    identity ? { case: "identity", value: identityName } : undefined,
    identity?.identity?.uid
  );

  return (
    <Stack component="section" aria-label="Environment identity" gap="xs">
      <Title order={5}>Identity</Title>
      {identityState.error ? (
        <Alert color="red" role="alert" title="Could not load the environment identity">
          {identityState.error}
        </Alert>
      ) : !identityState.identities ? (
        <Text role="status">Loading identity {identityName}…</Text>
      ) : !identity ? (
        <Alert color="red" role="alert" title="Environment identity is missing">
          Identity {identityName} is no longer available.
        </Alert>
      ) : policyState.error ? (
        <Alert color="red" role="alert" title="Could not load identity bindings">
          {policyState.error}
        </Alert>
      ) : !policyState.resources ? (
        <Text role="status">Loading bindings for {identityName}…</Text>
      ) : (
        <>
          <Text size="sm">
            {identityName}
            {identity.spec?.description ? ` · ${identity.spec.description}` : ""}
          </Text>
          <Text size="sm" c="dimmed">
            Policy bindings naming this environment&apos;s configured Identity.
          </Text>
          <BindingsTable
            bindings={policyState.resources.bindings}
            policySets={policyState.resources.sets}
            onRevoke={null}
            subjectKind="identity"
          />
        </>
      )}
    </Stack>
  );
}
