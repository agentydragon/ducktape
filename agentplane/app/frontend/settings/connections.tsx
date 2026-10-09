import { Alert, Button, Group, Select, Stack, Table, Text } from "@mantine/core";
import "./connections.css";
import { followStream, type StreamConnection } from "../live_stream";
import { type JSX, useCallback, useEffect, useState } from "react";

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
  const [rebinding, setRebinding] = useState<{ row: Connection; target: string; confirm: boolean } | null>(null);
  const [connection, setConnection] = useState<StreamConnection>({ phase: "connecting", since: Date.now() });

  const load = useCallback(async (): Promise<void> => {
    const [connections, callers] = await Promise.all([service.list(), service.callerServiceAccounts()]);
    setRows(connections);
    setAccounts(callers);
    setLoaded(true);
  }, [service]);

  const refresh = useCallback(async (): Promise<void> => {
    setBusy(true);
    setUnlinking(null);
    setRebinding(null);
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
  useEffect(() => {
    if (typeof EventSource === "undefined") return; // Non-browser unit test environment.
    return followStream("/connections/stream", {
      events: {
        snapshot: (message) => {
          setRows(JSON.parse(message.data) as Connection[]);
          setLoaded(true);
        },
      },
      onConnection: setConnection,
    });
  }, []);

  async function confirmRebind(): Promise<void> {
    if (rebinding === null) return;
    const target = accounts.find((account) => serviceAccountKey(account) === rebinding.target);
    if (!target) {
      setError("Selected ServiceAccount is no longer eligible. Refresh before retrying.");
      return;
    }
    setBusy(true);
    try {
      const updated = await service.rebind(rebinding.row, target);
      setRows((current) => current.map((row) => (row.id === updated.id ? updated : row)));
      setRebinding(null);
      setError(null);
    } catch (failure) {
      if (failure instanceof ConnectionRequestError && failure.status === 409) {
        setRebinding(null);
        try {
          await load();
          setError("Connection changed elsewhere. Review its new binding before trying again. Nothing was retried.");
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
      <Text c="dimmed" size="sm">
        Named external clients and the ServiceAccount they currently act as. Changing an account keeps existing client
        tokens usable for future requests; already-submitted Actions retain their original authority. Unlink revokes
        access without stopping already claimed work.
      </Text>
      {connection.phase === "reconnecting" && (
        <Text role="status">Connection lost; showing last OAuth clients. Reconnecting…</Text>
      )}
      {error && (
        <Alert color="red" role="alert">
          {error}
        </Alert>
      )}
      {!loaded && !error && <Text>Loading OAuth clients…</Text>}
      {loaded && rows.length === 0 && <Text c="dimmed">No OAuth clients yet. Authorize a client to create one.</Text>}
      {loaded && rows.length > 0 && (
        <Table className="agentplane-connections">
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
              const currentCaller = row.bound_caller ?? (unbound ? null : (grant?.caller ?? null));
              const currentKey = currentCaller ? serviceAccountKey(currentCaller) : null;
              const selected = rebinding?.row.id === row.id ? rebinding.target : currentKey;
              return (
                <Table.Tr key={row.id} data-connection-id={row.id}>
                  <Table.Td>
                    <Text fw={600} style={{ overflowWrap: "anywhere" }}>
                      {row.display_name}
                    </Text>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      Client ID · {grant?.client_id ?? "—"}
                    </Text>
                    <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                      Connection ID · {row.id}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    {currentCaller ? (
                      <Stack gap={2}>
                        <Text className="agentplane-connection-mobile-label" size="xs" fw={600}>
                          Service account
                        </Text>
                        <Select
                          aria-label={`Service account for ${row.display_name}`}
                          value={selected}
                          onChange={(value) => {
                            if (value) setRebinding({ row, target: value, confirm: false });
                          }}
                          data={[
                            ...accounts.map((account) => ({
                              value: serviceAccountKey(account),
                              label: serviceAccountKey(account),
                            })),
                            ...(!isEligibleCaller(currentCaller, accounts)
                              ? [{ value: currentKey!, label: currentKey!, disabled: true }]
                              : []),
                          ]}
                          disabled={busy || unbound}
                          size="xs"
                          className="agentplane-connection-account-select"
                        />
                        {!isEligibleCaller(currentCaller, accounts) && (
                          <Text size="xs" c="orange">
                            ServiceAccount not labeled as an Action caller
                          </Text>
                        )}
                        {rebinding?.row.id === row.id && rebinding.target !== currentKey && (
                          <Group gap="xs">
                            <Button size="xs" variant="subtle" onClick={() => setRebinding(null)}>
                              Cancel
                            </Button>
                            <Button
                              size="xs"
                              disabled={busy}
                              onClick={() => setRebinding({ ...rebinding, confirm: true })}
                            >
                              Apply
                            </Button>
                          </Group>
                        )}
                      </Stack>
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
      {rebinding?.confirm && (
        <Alert color="orange" title={`Change ${rebinding.row.display_name}'s ServiceAccount?`}>
          <Text size="sm">
            Existing client tokens will act as {rebinding.target} on future requests. Pending Actions admitted under the
            previous account will not be silently upgraded; already claimed executions continue.
          </Text>
          <Group mt="xs">
            <Button size="xs" variant="subtle" onClick={() => setRebinding(null)}>
              Cancel
            </Button>
            <Button size="xs" loading={busy} onClick={() => void confirmRebind()}>
              Confirm change
            </Button>
          </Group>
        </Alert>
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
