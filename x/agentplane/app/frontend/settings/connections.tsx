import { Alert, Button, Group, Stack, Table, Text, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import {
  ConnectionRequestError,
  connectionService,
  displayableError,
  isEligibleCaller,
  serviceAccountKey,
  type CallerServiceAccount,
  type Connection,
  type ConnectionService,
} from "../client";

type Grant = Connection["grants"][number];

/** The grant a row's Client/Service account columns describe: the connection's latest revision. */
function currentGrant(connection: Connection): Grant | undefined {
  return connection.grants.reduce<Grant | undefined>(
    (latest, grant) => (latest === undefined || grant.revision > latest.revision ? grant : latest),
    undefined
  );
}

export function Connections({ service = connectionService }: { service?: ConnectionService }): JSX.Element {
  const [rows, setRows] = useState<Connection[]>([]);
  const [accounts, setAccounts] = useState<CallerServiceAccount[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unlinking, setUnlinking] = useState<Connection | null>(null);

  const load = useCallback(async (): Promise<void> => {
    const [connections, callers] = await Promise.all([service.list(), service.callerServiceAccounts()]);
    setRows(connections);
    setAccounts(callers);
    setLoaded(true);
  }, [service]);

  const refresh = useCallback(async (): Promise<void> => {
    setBusy(true);
    setUnlinking(null);
    try {
      await load();
      setError(null);
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setBusy(false);
    }
  }, [load]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function confirmUnlink(): Promise<void> {
    if (unlinking === null) return;
    setBusy(true);
    try {
      const updated = await service.unbind(unlinking);
      setRows((current) => current.map((row) => (row.id === updated.id ? updated : row)));
      setUnlinking(null);
      setError(null);
    } catch (failure) {
      if (failure instanceof ConnectionRequestError && failure.status === 409) {
        setUnlinking(null);
        try {
          await load();
          setError(
            "Connection changed elsewhere. Latest state loaded; review it before trying again. Nothing was retried."
          );
        } catch (refreshFailure) {
          setError(
            `Connection changed elsewhere. Refresh failed: ${displayableError(refreshFailure)}. Nothing was retried.`
          );
        }
      } else {
        setError(displayableError(failure));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>OAuth clients</Title>
        <Button variant="light" loading={busy} onClick={() => void refresh()}>
          Refresh
        </Button>
      </Group>
      <Text c="dimmed" size="sm">
        Named external clients and the ServiceAccount their most recent grant acts as. Unlink revokes authority without
        deleting history or stopping already claimed work; changing the bound ServiceAccount requires a fresh
        authorization.
      </Text>
      {error && (
        <Alert color="red" role="alert">
          {error}
        </Alert>
      )}
      {!loaded && !error && <Text>Loading OAuth clients…</Text>}
      {loaded && rows.length === 0 && <Text c="dimmed">No OAuth clients yet. Authorize a client to create one.</Text>}
      {loaded && rows.length > 0 && (
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Client</Table.Th>
              <Table.Th>Service account</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {rows.map((row) => {
              const grant = currentGrant(row);
              const unbound = row.grants.every((candidate) => candidate.status === "revoked");
              return (
                <Table.Tr key={row.id} data-connection-id={row.id}>
                  <Table.Td>
                    <Text style={{ overflowWrap: "anywhere" }}>{grant?.client_id ?? "—"}</Text>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      {row.display_name} · {row.id}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    {grant ? (
                      <>
                        <Text>{serviceAccountKey(grant.caller)}</Text>
                        {!isEligibleCaller(grant.caller, accounts) && (
                          <Text size="xs" c="orange">
                            ServiceAccount not labeled as an Action caller
                          </Text>
                        )}
                      </>
                    ) : (
                      "—"
                    )}
                  </Table.Td>
                  <Table.Td>
                    {unlinking?.id === row.id ? (
                      <Group gap="xs" justify="flex-end" wrap="nowrap">
                        <Button variant="subtle" size="xs" disabled={busy} onClick={() => setUnlinking(null)}>
                          Cancel
                        </Button>
                        <Button color="red" size="xs" loading={busy} onClick={() => void confirmUnlink()}>
                          Confirm unlink
                        </Button>
                      </Group>
                    ) : (
                      <Group justify="flex-end">
                        <Button
                          color="red"
                          variant="light"
                          size="xs"
                          disabled={busy || unbound}
                          onClick={() => setUnlinking(row)}
                        >
                          Unlink
                        </Button>
                      </Group>
                    )}
                  </Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      )}
      {unlinking && (
        <Alert color="orange" title={`Unlink ${unlinking.display_name}?`}>
          Revoke this Connection’s active and pending grants. Its name, immutable IDs, and grant history remain. Already
          claimed executions are not stopped. Fresh authorization is required to regain access.
        </Alert>
      )}
    </Stack>
  );
}
