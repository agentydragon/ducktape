import { Alert, Badge, Button, Group, Paper, Stack, Text, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import { displayableError, mcpLinkageService, type McpLinkageService, type McpLinkageView } from "./client";

export function McpServers({ service = mcpLinkageService }: { service?: McpLinkageService }): JSX.Element {
  const [rows, setRows] = useState<McpLinkageView[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<{ serverId: string; operation: "link" | "disconnect" } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      setRows(await service.list());
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setLoading(false);
    }
  }, [service]);

  useEffect(() => {
    void load();
  }, [load]);

  async function link(row: McpLinkageView): Promise<void> {
    setBusy({ serverId: row.server_id, operation: "link" });
    setError(null);
    try {
      const flow = await service.start(row.server_id, row.scopes);
      window.location.assign(flow.authorization_url);
    } catch (failure) {
      setError(displayableError(failure));
      setBusy(null);
    }
  }

  async function disconnect(row: McpLinkageView): Promise<void> {
    setBusy({ serverId: row.server_id, operation: "disconnect" });
    setError(null);
    try {
      const updated = await service.disconnect(row.server_id);
      setRows((current) => current.map((item) => (item.server_id === updated.server_id ? updated : item)));
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>MCP servers</Title>
        <Button variant="light" loading={loading} disabled={busy !== null} onClick={() => void load()}>
          Refresh
        </Button>
      </Group>
      <Text c="dimmed" size="sm">
        Link one shared operator-managed OAuth account per configured MCP server. Agents never receive these
        credentials.
      </Text>
      {error && <Alert color="red">{error}</Alert>}
      {loading && <Text role="status">Loading MCP servers…</Text>}
      {rows.map((row) => (
        <Paper key={row.server_id} withBorder p="md">
          <Group justify="space-between" align="flex-start">
            <div>
              <Text fw={600}>{row.server_id}</Text>
              <Text size="sm" c="dimmed">
                {row.provider} · {row.server_url}
              </Text>
              <Text size="xs" c="dimmed">
                Scopes: {row.scopes.length ? row.scopes.join(", ") : "provider default"}
              </Text>
            </div>
            <Badge
              color={
                row.status === "linked"
                  ? "green"
                  : row.status === "degraded" || row.status === "expired"
                    ? "orange"
                    : "gray"
              }
            >
              {row.status}
            </Badge>
          </Group>
          <Group justify="flex-end" mt="sm">
            <Button
              loading={busy?.serverId === row.server_id && busy.operation === "link"}
              disabled={loading || busy !== null}
              onClick={() => void link(row)}
            >
              {row.status === "linked" ? "Reconnect" : "Link account"}
            </Button>
            {row.status === "linked" && (
              <Button
                color="red"
                variant="light"
                loading={busy?.serverId === row.server_id && busy.operation === "disconnect"}
                disabled={loading || busy !== null}
                onClick={() => void disconnect(row)}
              >
                Disconnect
              </Button>
            )}
          </Group>
        </Paper>
      ))}
      {!loading && !error && rows.length === 0 && <Text c="dimmed">No OAuth-capable MCP servers are configured.</Text>}
    </Stack>
  );
}
