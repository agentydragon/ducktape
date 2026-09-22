import { Alert, Badge, Button, Group, Paper, Stack, Text, Title } from "@mantine/core";
import { type JSX, useCallback, useEffect, useState } from "react";

import {
  type ActionGroupHealthService,
  actionGroupHealthService,
  type ActionGroupHealthView,
  displayableError,
  mcpLinkageService,
  type McpLinkageService,
  type McpLinkageView,
} from "../client";

type McpHealth = ActionGroupHealthView["health"];

type Row =
  | { kind: "oauth"; linkage: McpLinkageView; group: ActionGroupHealthView | null }
  | { kind: "health-only"; group: ActionGroupHealthView };

function mergeRows(groups: ActionGroupHealthView[], linkages: McpLinkageView[]): Row[] {
  const linkageBySid = new Map(linkages.map((linkage) => [linkage.server_id, linkage]));
  const matched = new Set<string>();
  const rows: Row[] = [];
  for (const group of groups) {
    const linkage = group.oauth_server_id != null ? linkageBySid.get(group.oauth_server_id) : undefined;
    if (linkage) {
      matched.add(linkage.server_id);
      rows.push({ kind: "oauth", linkage, group });
    } else {
      rows.push({ kind: "health-only", group });
    }
  }
  for (const linkage of linkages) {
    if (!matched.has(linkage.server_id)) rows.push({ kind: "oauth", linkage, group: null });
  }
  return rows;
}

function rowKey(row: Row): string {
  return row.kind === "oauth" ? `linkage:${row.linkage.server_id}` : `group:${row.group.key}`;
}

function lifecycleColor(health: McpHealth): "green" | "orange" | "gray" {
  if (health == null) return "gray";
  switch (health.state) {
    case "available":
      return "green";
    case "draining":
    case "stopped":
      return health.reason === "supervisor_stopped" ? "orange" : "gray";
    default:
      return "orange"; // disconnected, connecting, discovering
  }
}

function LifecycleBadge({ health }: { health: McpHealth }): JSX.Element {
  return <Badge color={lifecycleColor(health)}>{health == null ? "pending" : (health.reason ?? health.state)}</Badge>;
}

export function McpServers({
  service = mcpLinkageService,
  healthService = actionGroupHealthService,
}: {
  service?: McpLinkageService;
  healthService?: ActionGroupHealthService;
}): JSX.Element {
  const [linkages, setLinkages] = useState<McpLinkageView[]>([]);
  const [groups, setGroups] = useState<ActionGroupHealthView[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<{ serverId: string; operation: "link" | "disconnect" } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    setWarnings([]);
    const [linkageResult, healthResult] = await Promise.allSettled([service.list(), healthService.list()]);
    const nextWarnings: string[] = [];
    if (linkageResult.status === "fulfilled") setLinkages(linkageResult.value);
    else {
      setLinkages([]);
      nextWarnings.push(`OAuth-linked servers: ${displayableError(linkageResult.reason)}`);
    }
    if (healthResult.status === "fulfilled") setGroups(healthResult.value);
    else {
      setGroups([]);
      nextWarnings.push(`Live connection health: ${displayableError(healthResult.reason)}`);
    }
    setWarnings(nextWarnings);
    setLoading(false);
  }, [service, healthService]);

  useEffect(() => {
    void load();
  }, [load]);

  async function link(linkage: McpLinkageView): Promise<void> {
    setBusy({ serverId: linkage.server_id, operation: "link" });
    setError(null);
    try {
      const flow = await service.start(linkage.server_id, linkage.scopes);
      window.location.assign(flow.authorization_url);
    } catch (failure) {
      setError(displayableError(failure));
      setBusy(null);
    }
  }

  async function disconnect(linkage: McpLinkageView): Promise<void> {
    setBusy({ serverId: linkage.server_id, operation: "disconnect" });
    setError(null);
    try {
      const updated = await service.disconnect(linkage.server_id);
      setLinkages((current) => current.map((item) => (item.server_id === updated.server_id ? updated : item)));
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setBusy(null);
    }
  }

  const rows = mergeRows(groups, linkages);

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>MCP servers</Title>
        <Button variant="light" loading={loading} disabled={busy !== null} onClick={() => void load()}>
          Refresh
        </Button>
      </Group>
      <Text c="dimmed" size="sm">
        Every configured MCP server, oauth-linked or not. Link one shared operator-managed OAuth account per
        oauth-linked server; Agents never receive these credentials.
      </Text>
      {error && <Alert color="red">{error}</Alert>}
      {warnings.length > 0 && (
        <Alert color="orange">
          {warnings.map((warning) => (
            <Text key={warning} size="sm">
              {warning}
            </Text>
          ))}
        </Alert>
      )}
      {loading && <Text role="status">Loading MCP servers…</Text>}
      {rows.map((row) => (
        <Paper key={rowKey(row)} withBorder p="md">
          {row.kind === "oauth" ? (
            <>
              <Group justify="space-between" align="flex-start">
                <div>
                  <Text fw={600}>{row.linkage.server_id}</Text>
                  <Text size="sm" c="dimmed">
                    {row.linkage.provider} · {row.linkage.server_url}
                  </Text>
                  <Text size="xs" c="dimmed">
                    Scopes: {row.linkage.scopes.length ? row.linkage.scopes.join(", ") : "provider default"}
                  </Text>
                </div>
                <Group gap="xs">
                  <Badge
                    color={
                      row.linkage.status === "linked"
                        ? "green"
                        : row.linkage.status === "degraded" || row.linkage.status === "expired"
                          ? "orange"
                          : "gray"
                    }
                  >
                    {row.linkage.status}
                  </Badge>
                  {row.group != null && row.linkage.status !== "unlinked" && (
                    <LifecycleBadge health={row.group.health} />
                  )}
                </Group>
              </Group>
              <Group justify="flex-end" mt="sm">
                <Button
                  loading={busy?.serverId === row.linkage.server_id && busy.operation === "link"}
                  disabled={loading || busy !== null}
                  onClick={() => void link(row.linkage)}
                >
                  {row.linkage.status === "linked" ? "Reconnect" : "Link account"}
                </Button>
                {row.linkage.status === "linked" && (
                  <Button
                    color="red"
                    variant="light"
                    loading={busy?.serverId === row.linkage.server_id && busy.operation === "disconnect"}
                    disabled={loading || busy !== null}
                    onClick={() => void disconnect(row.linkage)}
                  >
                    Disconnect
                  </Button>
                )}
              </Group>
            </>
          ) : (
            <Group justify="space-between" align="flex-start">
              <div>
                <Text fw={600}>{row.group.key}</Text>
                <Text size="sm" c="dimmed">
                  {row.group.executor_description}
                </Text>
              </div>
              <LifecycleBadge health={row.group.health} />
            </Group>
          )}
        </Paper>
      ))}
      {!loading && warnings.length === 0 && rows.length === 0 && <Text c="dimmed">No MCP servers are configured.</Text>}
    </Stack>
  );
}
