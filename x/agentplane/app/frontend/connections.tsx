import { Alert, Badge, Button, Group, Paper, Stack, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import {
  ConnectionRequestError,
  connectionService,
  displayableError,
  type Connection,
  type ConnectionIdentity,
  type ConnectionService,
} from "./client";

type Edit = { kind: "rename" | "unbind"; connection: Connection };

export function Connections({ service = connectionService }: { service?: ConnectionService }): JSX.Element {
  const [rows, setRows] = useState<Connection[]>([]);
  const [identities, setIdentities] = useState<Record<string, ConnectionIdentity>>({});
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [edit, setEdit] = useState<Edit | null>(null);
  const [name, setName] = useState("");

  const load = useCallback(async (): Promise<void> => {
    const [connections, catalog] = await Promise.all([service.list(), service.identities()]);
    setRows(connections);
    setIdentities(catalog);
    setLoaded(true);
  }, [service]);

  const refresh = useCallback(async (): Promise<void> => {
    setBusy(true);
    setEdit(null);
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

  async function save(): Promise<void> {
    if (edit === null) return;
    setBusy(true);
    try {
      const updated =
        edit.kind === "rename"
          ? await service.rename(edit.connection, name.trim())
          : await service.unbind(edit.connection);
      setRows((current) => current.map((row) => (row.id === updated.id ? updated : row)));
      setEdit(null);
      setError(null);
    } catch (failure) {
      if (failure instanceof ConnectionRequestError && failure.status === 409) {
        setEdit(null);
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
        <Title order={2}>Connections</Title>
        <Button variant="light" loading={busy} onClick={() => void refresh()}>
          Refresh
        </Button>
      </Group>
      <Text c="dimmed" size="sm">
        Named external clients and their grant history. Identity availability and grant status are separate. Unbind
        revokes authority without deleting history or stopping already claimed work.
      </Text>
      {error && (
        <Alert color="red" role="alert">
          {error}
        </Alert>
      )}
      {!loaded && !error && <Text>Loading Connections…</Text>}
      {loaded && rows.length === 0 && <Text c="dimmed">No Connections yet. Authorize a client to create one.</Text>}
      {rows.map((row) => (
        <Paper key={row.id} withBorder p="md" data-connection-id={row.id}>
          <Stack gap="sm">
            <Group justify="space-between" align="flex-start">
              <div>
                <Text fw={600}>{row.display_name}</Text>
                <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                  Connection {row.id} · version {row.version}
                </Text>
              </div>
              {row.grants.every((grant) => grant.status === "revoked") && <Badge color="gray">Unbound</Badge>}
            </Group>
            {row.grants.map((grant) => (
              <Paper key={grant.id} withBorder p="sm">
                <Stack gap={4}>
                  <Group gap="xs">
                    <Badge color={grant.status === "active" ? "blue" : grant.status === "pending" ? "yellow" : "gray"}>
                      Grant {grant.status}
                    </Badge>
                    <Text size="sm">Identity: {grant.identity_id}</Text>
                    <Text size="sm" c={identities[grant.identity_id]?.enabled ? "dimmed" : "orange"}>
                      (
                      {identities[grant.identity_id] === undefined
                        ? "not configured"
                        : identities[grant.identity_id].enabled
                          ? "enabled"
                          : "disabled"}
                      )
                    </Text>
                  </Group>
                  <Text size="xs" style={{ overflowWrap: "anywhere" }}>
                    Client: {grant.client_id} · Issuer: {grant.issuer}
                  </Text>
                  <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
                    Grant {grant.id} · revision {grant.revision}
                  </Text>
                </Stack>
              </Paper>
            ))}
            {edit?.connection.id === row.id ? (
              <Stack gap="sm">
                {edit.kind === "rename" ? (
                  <TextInput
                    label="Connection name"
                    value={name}
                    maxLength={200}
                    disabled={busy}
                    onChange={(event) => setName(event.currentTarget.value)}
                  />
                ) : (
                  <Alert color="orange" title={`Unbind ${edit.connection.display_name}?`}>
                    Revoke this Connection’s active and pending grants. Its name, immutable IDs, and grant history
                    remain. Already claimed executions are not stopped. Fresh authorization is required to regain
                    access.
                  </Alert>
                )}
                <Group justify="flex-end">
                  <Button variant="subtle" disabled={busy} onClick={() => setEdit(null)}>
                    Cancel
                  </Button>
                  <Button
                    color={edit.kind === "unbind" ? "red" : "blue"}
                    loading={busy}
                    disabled={edit.kind === "rename" && name.trim().length === 0}
                    onClick={() => void save()}
                  >
                    {edit.kind === "rename" ? "Save name" : "Confirm unbind"}
                  </Button>
                </Group>
              </Stack>
            ) : (
              <Group justify="flex-end">
                <Button
                  variant="light"
                  disabled={busy}
                  onClick={() => {
                    setName(row.display_name);
                    setEdit({ kind: "rename", connection: row });
                  }}
                >
                  Rename
                </Button>
                <Button
                  color="red"
                  variant="light"
                  disabled={busy || row.grants.every((grant) => grant.status === "revoked")}
                  onClick={() => setEdit({ kind: "unbind", connection: row })}
                >
                  Unbind
                </Button>
              </Group>
            )}
          </Stack>
        </Paper>
      ))}
    </Stack>
  );
}
