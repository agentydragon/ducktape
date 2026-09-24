import { Alert, Badge, Box, Button, Group, Paper, Stack, Text, Title } from "@mantine/core";
import { type JSX, type ReactNode, useCallback, useEffect, useState } from "react";

import {
  type ActionGroupService,
  actionGroupService,
  type ActionGroupView,
  displayableError,
  mcpLinkageService,
  type McpLinkageService,
  type McpLinkageView,
} from "../client";

type McpHealth = ActionGroupView["health"];

type Row =
  | { kind: "oauth"; linkage: McpLinkageView; group: ActionGroupView | null }
  | { kind: "health-only"; group: ActionGroupView };

// /action-groups returns every configured group, sandbox-kind included; this page only cares
// about mcp-kind ones. Every mcp-kind group whose config carries a server_id is guaranteed (by a
// backend validation invariant, ActionCatalog._server_id_matches_group_key) to have that
// server_id equal its own key -- so an oauth-linked group's key doubles as the join key, with no
// separate field needed.
function mergeRows(groups: ActionGroupView[], linkages: McpLinkageView[]): Row[] {
  const linkageBySid = new Map(linkages.map((linkage) => [linkage.server_id, linkage]));
  const matched = new Set<string>();
  const rows: Row[] = [];
  for (const group of groups) {
    if (group.executor_kind !== "mcp") continue;
    const linkage = linkageBySid.get(group.key);
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

// Label/badge pairs in two columns, so the badges line up however the row wraps.
function States({ children }: { children: ReactNode }): JSX.Element {
  return (
    <Box style={{ display: "grid", gridTemplateColumns: "auto auto", gap: "4px 6px", alignItems: "center" }}>
      {children}
    </Box>
  );
}

function StateLabel({ children }: { children: ReactNode }): JSX.Element {
  return (
    <Text size="xs" c="dimmed">
      {children}
    </Text>
  );
}

function ConnectionState({ health }: { health: McpHealth }): JSX.Element {
  return (
    <>
      <StateLabel>Connection</StateLabel>
      <Badge color={lifecycleColor(health)}>{health == null ? "pending" : (health.reason ?? health.state)}</Badge>
    </>
  );
}

function HealthDetail({ health }: { health: McpHealth }): JSX.Element | null {
  if (health?.detail == null) return null;
  return (
    <Text size="sm" c="orange.8" mt="xs" style={{ overflowWrap: "anywhere" }}>
      {health.detail}
    </Text>
  );
}

export function McpServers({
  service = mcpLinkageService,
  groupService = actionGroupService,
}: {
  service?: McpLinkageService;
  groupService?: ActionGroupService;
}): JSX.Element {
  const [linkages, setLinkages] = useState<McpLinkageView[]>([]);
  const [groups, setGroups] = useState<ActionGroupView[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<{ serverId: string; operation: "link" | "disconnect" } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    setWarnings([]);
    const [linkageResult, groupResult] = await Promise.allSettled([service.list(), groupService.list()]);
    const nextWarnings: string[] = [];
    if (linkageResult.status === "fulfilled") setLinkages(linkageResult.value);
    else {
      setLinkages([]);
      nextWarnings.push(`OAuth-linked servers: ${displayableError(linkageResult.reason)}`);
    }
    if (groupResult.status === "fulfilled") setGroups(groupResult.value);
    else {
      setGroups([]);
      nextWarnings.push(`Live connection health: ${displayableError(groupResult.reason)}`);
    }
    setWarnings(nextWarnings);
    setLoading(false);
  }, [service, groupService]);

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
        <Paper key={rowKey(row)} data-mcp-server={rowKey(row)} withBorder p="md">
          {row.kind === "oauth" ? (
            <>
              <Group justify="space-between" align="flex-start">
                <div>
                  <Text fw={600}>{row.linkage.server_id}</Text>
                  <Text size="sm" c="dimmed">
                    {row.linkage.server_url}
                  </Text>
                  <Text size="xs" c="dimmed">
                    Scopes: {row.linkage.scopes.length ? row.linkage.scopes.join(", ") : "provider default"}
                  </Text>
                </div>
                <States>
                  <StateLabel>OAuth link</StateLabel>
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
                    <ConnectionState health={row.group.health} />
                  )}
                </States>
              </Group>
              {row.group != null && row.linkage.status !== "unlinked" && <HealthDetail health={row.group.health} />}
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
            <>
              <Group justify="space-between" align="flex-start">
                <div>
                  <Text fw={600}>{row.group.key}</Text>
                  <Text size="sm" c="dimmed">
                    {row.group.executor_description}
                  </Text>
                </div>
                <States>
                  <ConnectionState health={row.group.health} />
                </States>
              </Group>
              <HealthDetail health={row.group.health} />
            </>
          )}
        </Paper>
      ))}
      {!loading && warnings.length === 0 && rows.length === 0 && <Text c="dimmed">No MCP servers are configured.</Text>}
    </Stack>
  );
}
