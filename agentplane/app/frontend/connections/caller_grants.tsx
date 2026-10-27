import { Alert, Button, Group, Stack, Text, Title } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import { ActionPolicySection } from "../actions/policy";
import { BINDING_GRANT_HELP, BindingsTable } from "../bindings/table";
import { type CallerServiceAccount, serviceAccountKey, type CallerGrantReader, type CallerGrantView } from "./models";
import { displayableError } from "../client";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";
import { readCallerGrants } from "./caller_grants_client";

/** Read-only account snapshot. Sandbox pages use the same renderers with their live data. */
export function CallerGrants({
  account,
  read = readCallerGrants,
}: {
  account: CallerServiceAccount;
  read?: CallerGrantReader;
}): JSX.Element {
  const [view, setView] = useState<CallerGrantView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const { namespace, name } = account;
  useEffect(() => {
    const controller = new AbortController();
    setView(null);
    setError(null);
    void read({ namespace, name }, controller.signal).then(
      (result) => {
        if (!controller.signal.aborted) setView(result);
      },
      (failure) => {
        if (!controller.signal.aborted) setError(displayableError(failure));
      }
    );
    return () => controller.abort();
  }, [namespace, name, read, revision]);

  return (
    <Stack component="section" aria-label={`Grants for ${serviceAccountKey(account)}`} gap="sm" py="sm">
      <Group justify="space-between">
        <Title order={5}>Grants for {serviceAccountKey(account)}</Title>
        <Button size="xs" variant="light" onClick={() => setRevision((value) => value + 1)}>
          Refresh grants
        </Button>
      </Group>
      <Text size="sm" c="dimmed">
        Policy bindings shared by all clients and workloads using this ServiceAccount. This is a snapshot; refresh to
        see changes. Kubernetes RBAC is not included.
      </Text>
      {error && (
        <Alert color="red" role="alert">
          Could not load grants: {error}
        </Alert>
      )}
      {!view && !error && <Text role="status">Loading grants…</Text>}
      {view && (
        <>
          <Group gap={4}>
            <Title order={6}>Bindings</Title>
            <HelpTooltip topic="Bindings" description={BINDING_GRANT_HELP} />
          </Group>
          <BindingsTable bindings={view.bindings} onRevoke={null} />
          <ActionPolicySection policy={view.action_policy} />
        </>
      )}
    </Stack>
  );
}
