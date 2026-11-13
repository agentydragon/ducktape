import { Alert, Button, Group, Select, Stack, Table, Text } from "@mantine/core";
import "./management.css";
import { type StreamConnection } from "../stream_reader";
import { Fragment, type JSX, useCallback, useEffect, useState } from "react";

import { displayableError, HttpError } from "../client";
import {
  connectionIdentities,
  listConnections,
  readConnectionWatchFrame,
  rebindConnection,
  renameConnection,
  unbindConnection,
} from "./connection_service_client";

import { followWatch } from "../watch_stream";

import { CallerGrants } from "./caller_grants";
import { InlineName } from "../inline_name";
import { GrantStatus, type Connection } from "../../../mcp_edge/connection_service_pb";
import type { Identity } from "../../../proto/identity_pb";

type Grant = Connection["grants"][number];

function identityKey(identity: Identity): string {
  return `${identity.name}/${identity.uid}`;
}

/** The grant described by a row: the connection's latest revision. */
function currentGrant(connection: Connection): Grant | undefined {
  return connection.grants.reduce<Grant | undefined>(
    (latest, grant) => (latest === undefined || grant.revision > latest.revision ? grant : latest),
    undefined
  );
}

export function Connections(): JSX.Element {
  const [snapshot, setRows] = useState<Connection[] | null>(null);
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  const rows = snapshot ?? [];
  const loaded = snapshot !== null;
  const [identities, setIdentities] = useState<Identity[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unlinking, setUnlinking] = useState<Connection | null>(null);
  const [expandedGrants, setExpandedGrants] = useState<string | null>(null);
  const [rebinding, setRebinding] = useState<{ row: Connection; target: string; confirm: boolean } | null>(null);

  useEffect(() => {
    let snapshot: Connection[] = [];
    let readingSnapshot = false;
    return followWatch("/upstream/connections/v1/operator/connections:watch", {
      onOpen: () => {
        snapshot = [];
        readingSnapshot = true;
      },
      onFrame: (value) => {
        const frame = readConnectionWatchFrame(value);
        switch (frame.kind) {
          case "snapshot":
            if (!readingSnapshot) throw new Error("ConnectionService sent a snapshot outside its initial replay");
            snapshot.push(...frame.connections);
            return;
          case "snapshot_complete":
            if (!readingSnapshot) throw new Error("ConnectionService completed a snapshot that was not open");
            readingSnapshot = false;
            setRows(snapshot);
            return;
          case "upsert":
            if (readingSnapshot) throw new Error("ConnectionService sent an update before completing its snapshot");
            setRows((current) => {
              if (current === null) return [frame.connection];
              const found = current.some((row) => row.id === frame.connection.id);
              return found
                ? current.map((row) => (row.id === frame.connection.id ? frame.connection : row))
                : [...current, frame.connection];
            });
            return;
          case "reconnect_required":
            // The frame is followed by EOF; followWatch reopens with a fresh snapshot.
            return;
        }
      },
      onConnection: setConnection,
    });
  }, [setRows]);

  const load = useCallback(async (): Promise<void> => {
    const [connections, liveIdentities] = await Promise.all([listConnections(), connectionIdentities()]);
    setRows(connections);
    setIdentities(liveIdentities);
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

  function replaceRow(updated: Connection): void {
    setRows((current) => current?.map((row) => (row.id === updated.id ? updated : row)) ?? null);
  }

  /** After a 409, the Connection having changed since it was read: load its latest state and say so
   * with `advice`. The change is never retried. */
  async function reloadAfterConflict(advice: string): Promise<void> {
    try {
      await load();
      setError(`Connection changed elsewhere. ${advice} Nothing was retried.`);
    } catch (refreshFailure) {
      setError(
        `Connection changed elsewhere. Refresh failed: ${displayableError(refreshFailure)}. Nothing was retried.`
      );
    }
  }

  async function rename(row: Connection, name: string | null): Promise<void> {
    // A Connection always has a name: a blank one leaves the stored name standing.
    if (name === null) return;
    setBusy(true);
    try {
      replaceRow(await renameConnection(row, name));
      setError(null);
    } catch (failure) {
      if (failure instanceof HttpError && failure.status === 409) {
        await reloadAfterConflict("Latest state loaded; review it before renaming again.");
      } else {
        setError(displayableError(failure));
      }
    } finally {
      setBusy(false);
    }
  }

  async function confirmRebind(): Promise<void> {
    if (rebinding === null) return;
    const target = identities.find((identity) => identityKey(identity) === rebinding.target);
    if (!target) {
      setError("Selected Identity is no longer live. Refresh before retrying.");
      return;
    }
    setBusy(true);
    try {
      replaceRow(await rebindConnection(rebinding.row, target));
      setRebinding(null);
      setError(null);
    } catch (failure) {
      if (failure instanceof HttpError && failure.status === 409) {
        setRebinding(null);
        await reloadAfterConflict("Review its new binding before trying again.");
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
      replaceRow(await unbindConnection(unlinking));
      setUnlinking(null);
      setError(null);
    } catch (failure) {
      if (failure instanceof HttpError && failure.status === 409) {
        setUnlinking(null);
        await reloadAfterConflict("Latest state loaded; review it before trying again.");
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
        Named external clients and the Identity they currently act as. A legacy ServiceAccount binding is paused until
        you explicitly choose a live Identity. Rebinding changes future requests made with existing client tokens;
        already-submitted Actions retain their original authority. Unlink revokes access without stopping already
        claimed work.
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
              <Table.Th>Identity</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {rows.map((row) => {
              const grant = currentGrant(row);
              const currentKey = row.boundIdentity ? identityKey(row.boundIdentity) : null;
              const selected = rebinding?.row.id === row.id ? rebinding.target : currentKey;
              const currentIdentityIsLive =
                row.boundIdentity !== undefined &&
                identities.some(
                  (identity) => identity.name === row.boundIdentity?.name && identity.uid === row.boundIdentity?.uid
                );
              return (
                <Fragment key={row.id}>
                  <Table.Tr data-connection-id={row.id}>
                    <Table.Td>
                      <InlineName
                        label="Client name"
                        name={row.displayName}
                        maxLength={200}
                        disabled={busy}
                        className="agentplane-connection-name"
                        onCommit={(name) => void rename(row, name)}
                      />
                      <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                        Client ID · {grant?.clientId ?? "—"}
                      </Text>
                      <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                        Connection ID · {row.id}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      {row.identityBindingRequired && (
                        <Alert color="orange" title="Identity binding required">
                          <Text size="sm">
                            This legacy Connection is paused. Its ServiceAccount binding is retained only as historical
                            attribution
                            {row.legacyBoundCaller
                              ? ` (${row.legacyBoundCaller.namespace}/${row.legacyBoundCaller.name})`
                              : ""}
                            . Choose a live Identity and confirm the rebind to resume its existing grants.
                          </Text>
                        </Alert>
                      )}
                      {row.boundIdentity || row.identityBindingRequired ? (
                        <Stack gap={2}>
                          <Text className="agentplane-connection-mobile-label" size="xs" fw={600}>
                            Identity
                          </Text>
                          <Select
                            aria-label={`Identity for ${row.displayName}`}
                            value={selected}
                            onChange={(value) => {
                              if (value) setRebinding({ row, target: value, confirm: false });
                            }}
                            data={[
                              ...identities.map((identity) => ({
                                value: identityKey(identity),
                                label: `${identity.name} · UID ${identity.uid}`,
                              })),
                              ...(row.boundIdentity && !currentIdentityIsLive
                                ? [
                                    {
                                      value: currentKey!,
                                      label: `${row.boundIdentity.name} · UID ${row.boundIdentity.uid} (not live)`,
                                      disabled: true,
                                    },
                                  ]
                                : []),
                            ]}
                            disabled={busy}
                            size="xs"
                            className="agentplane-connection-identity-select"
                          />
                          {row.boundIdentity && !currentIdentityIsLive && (
                            <Text size="xs" c="orange">
                              Bound Identity is missing, deleting, or has been replaced
                            </Text>
                          )}
                          {currentIdentityIsLive && (
                            <Button
                              size="xs"
                              variant="subtle"
                              aria-expanded={expandedGrants === row.id}
                              aria-controls={`caller-grants-${row.id}`}
                              onClick={() => setExpandedGrants(expandedGrants === row.id ? null : row.id)}
                            >
                              {expandedGrants === row.id ? "Hide grants" : "View grants"}
                            </Button>
                          )}
                          {rebinding?.row.id === row.id &&
                            (rebinding.target !== currentKey || row.identityBindingRequired) &&
                            !rebinding.confirm && (
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
                            <Alert color="orange" title={`Change ${row.displayName}'s Identity?`}>
                              <Text size="sm">
                                Existing client tokens will act as Identity {rebinding.target} on future requests.
                                Historical grant and consent records stay unchanged. Pending Actions are not rewritten;
                                already claimed executions continue.
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
                        <Text c="dimmed">Unbound</Text>
                      )}
                    </Table.Td>
                    <Table.Td>
                      {unlinking?.id === row.id ? (
                        <Alert color="orange" title={`Unlink ${row.displayName}?`}>
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
                            disabled={busy || row.grants.every((candidate) => candidate.status === GrantStatus.REVOKED)}
                            onClick={() => setUnlinking(row)}
                          >
                            Unlink
                          </Button>
                        </Group>
                      )}
                    </Table.Td>
                  </Table.Tr>
                  {expandedGrants === row.id && row.boundIdentity && currentIdentityIsLive && (
                    <Table.Tr>
                      <Table.Td colSpan={3}>
                        <div id={`caller-grants-${row.id}`}>
                          <CallerGrants
                            key={`${row.boundIdentity.name}/${row.boundIdentity.uid}`}
                            identity={row.boundIdentity}
                          />
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
