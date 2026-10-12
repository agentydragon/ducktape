import { Alert, Button, Group, Select, Stack, Table, Text } from "@mantine/core";
import "./connections.css";
import { useSnapshotStream } from "../live_stream";
import { Fragment, type JSX, useCallback, useEffect, useState } from "react";

import {
  connectionServiceAccounts,
  displayableError,
  HttpError,
  isEligibleCaller,
  listConnections,
  rebindConnection,
  serviceAccountKey,
  type CallerServiceAccount,
  type CallerGrantReader,
  readCallerGrants,
  type Connection,
  unbindConnection,
} from "../client";

import { CallerGrants } from "../caller_grants";

type Grant = Connection["grants"][number];

/** The grant a row's Client/Service account columns describe: the connection's latest revision. */
function currentGrant(connection: Connection): Grant | undefined {
  return connection.grants.reduce<Grant | undefined>(
    (latest, grant) => (latest === undefined || grant.revision > latest.revision ? grant : latest),
    undefined
  );
}

export function Connections({ readGrants = readCallerGrants }: { readGrants?: CallerGrantReader }): JSX.Element {
  const [expandedGrants, setExpandedGrants] = useState<string | null>(null);
  const { snapshot, setSnapshot: setRows, connection } = useSnapshotStream<Connection[]>("/connections/stream");
  const rows = snapshot ?? [];
  const loaded = snapshot !== null;
  const [accounts, setAccounts] = useState<CallerServiceAccount[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unlinking, setUnlinking] = useState<Connection | null>(null);
  const [rebinding, setRebinding] = useState<{ row: Connection; target: string; confirm: boolean } | null>(null);

  const load = useCallback(async (): Promise<void> => {
    const [connections, callers] = await Promise.all([listConnections(), connectionServiceAccounts()]);
    setRows(connections);
    setAccounts(callers);
  }, [setRows]);

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

  async function confirmRebind(): Promise<void> {
    if (rebinding === null) return;
    const target = accounts.find((account) => serviceAccountKey(account) === rebinding.target);
    if (!target) {
      setError("Selected ServiceAccount is no longer eligible. Refresh before retrying.");
      return;
    }
    setBusy(true);
    try {
      const updated = await rebindConnection(rebinding.row, target);
      setRows((current) => current?.map((row) => (row.id === updated.id ? updated : row)) ?? null);
      setRebinding(null);
      setError(null);
    } catch (failure) {
      if (failure instanceof HttpError && failure.status === 409) {
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
      const updated = await unbindConnection(unlinking);
      setRows((current) => current?.map((row) => (row.id === updated.id ? updated : row)) ?? null);
      setUnlinking(null);
      setError(null);
    } catch (failure) {
      if (failure instanceof HttpError && failure.status === 409) {
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
              const currentKey = row.bound_caller ? serviceAccountKey(row.bound_caller) : null;
              const selected = rebinding?.row.id === row.id ? rebinding.target : currentKey;
              return (
                <Fragment key={row.id}>
                  <Table.Tr data-connection-id={row.id}>
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
                      {row.bound_caller ? (
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
                              ...(!isEligibleCaller(row.bound_caller, accounts)
                                ? [{ value: currentKey!, label: currentKey!, disabled: true }]
                                : []),
                            ]}
                            disabled={busy}
                            size="xs"
                            className="agentplane-connection-account-select"
                          />
                          <Button
                            size="compact-xs"
                            variant="subtle"
                            aria-expanded={expandedGrants === row.id}
                            aria-controls={`caller-grants-${row.id}`}
                            onClick={() => setExpandedGrants(expandedGrants === row.id ? null : row.id)}
                          >
                            {expandedGrants === row.id ? "Hide grants" : "View grants"}
                          </Button>
                          {!isEligibleCaller(row.bound_caller, accounts) && (
                            <Text size="xs" c="orange">
                              ServiceAccount not labeled as an Action caller
                            </Text>
                          )}
                          {rebinding?.row.id === row.id && rebinding.target !== currentKey && !rebinding.confirm && (
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
                          {rebinding?.confirm && rebinding.row.id === row.id && (
                            <Alert color="orange" title={`Change ${row.display_name}'s ServiceAccount?`}>
                              <Text size="sm">
                                Existing client tokens will act as {rebinding.target} on future requests. Pending
                                Actions admitted under the previous account will not be silently upgraded; already
                                claimed executions continue.
                              </Text>
                              <Group mt="xs">
                                <Button size="xs" variant="subtle" disabled={busy} onClick={() => setRebinding(null)}>
                                  Cancel
                                </Button>
                                <Button size="xs" loading={busy} onClick={() => void confirmRebind()}>
                                  Confirm change
                                </Button>
                              </Group>
                            </Alert>
                          )}
                        </Stack>
                      ) : (
                        "—"
                      )}
                    </Table.Td>
                    <Table.Td>
                      {unlinking?.id === row.id ? (
                        <Alert color="orange" title={`Unlink ${row.display_name}?`}>
                          <Text size="sm">
                            Revoke this Connection’s active and pending grants. Its name, immutable IDs, and grant
                            history remain. Already claimed executions are not stopped. Fresh authorization is required
                            to regain access.
                          </Text>
                          <Group mt="xs" justify="flex-end">
                            <Button variant="subtle" size="xs" disabled={busy} onClick={() => setUnlinking(null)}>
                              Cancel
                            </Button>
                            <Button color="red" size="xs" loading={busy} onClick={() => void confirmUnlink()}>
                              Confirm unlink
                            </Button>
                          </Group>
                        </Alert>
                      ) : (
                        <Group justify="flex-end">
                          <Button
                            color="red"
                            variant="light"
                            size="xs"
                            disabled={busy || row.grants.every((candidate) => candidate.status === "revoked")}
                            onClick={() => setUnlinking(row)}
                          >
                            Unlink
                          </Button>
                        </Group>
                      )}
                    </Table.Td>
                  </Table.Tr>
                  {expandedGrants === row.id && row.bound_caller && (
                    <Table.Tr>
                      <Table.Td colSpan={3}>
                        <div id={`caller-grants-${row.id}`}>
                          <CallerGrants key={currentKey} account={row.bound_caller} read={readGrants} />
                        </div>
                      </Table.Td>
                    </Table.Tr>
                  )}
                </Fragment>
              );
            })}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}
