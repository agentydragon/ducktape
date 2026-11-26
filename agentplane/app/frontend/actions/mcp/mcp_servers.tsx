import { Alert, Badge, Box, Button, Group, Paper, Stack, Text } from "@mantine/core";
import { type JSX, type ReactNode, useState } from "react";
import { toJson } from "@bufbuild/protobuf";
import { TimestampSchema, type Timestamp } from "@bufbuild/protobuf/wkt";
import {
  useWatchCollection,
  type CollectionConfig,
  type CollectionFrame,
  type CollectionWatchHandlers,
} from "../../transport/watch_collection";

import { displayableError } from "../../transport/client";
import {
  ExecutorKind,
  InputSchemaValidationStatus,
  McpLifecycle,
  McpUnavailableReason,
  type ActionGroup,
  type ActionGroupChunk,
} from "../../../../action_service/api/contracts/action_service_pb";
import {
  McpLinkageStatus,
  McpRefreshAction,
  type McpServerLinkage,
  type McpServerLinkageChunk,
} from "../../../../action_service/api/contracts/operator_action_service_pb";
import { followActionGroups } from "../client";
import { mcpLifecycleLabel, mcpUnavailableReasonLabel } from "../presentation";
import { TimeAgo } from "../../ui/time_ago";
import { disconnectMcpLinkage, followMcpLinkages, startMcpLinkage } from "./mcp_linkage_client";

type McpHealth = ActionGroup["health"];

function SchemaWarnings({ group }: { group: ActionGroup }): JSX.Element | null {
  const warnings = group.actions.filter(
    (action) => action.inputSchemaValidation?.status === InputSchemaValidationStatus.NOT_ENFORCED
  );
  if (warnings.length === 0) return null;
  return (
    <Alert color="yellow" title="Some tool arguments are not validated" mt="sm">
      {warnings.map((action) => (
        <Text key={action.name} size="sm">
          {action.name}:{" "}
          {action.inputSchemaValidation?.detail ??
            "Agentplane cannot validate arguments against this tool's input schema."}
        </Text>
      ))}
    </Alert>
  );
}

type Row =
  { kind: "oauth"; linkage: McpServerLinkage; group: ActionGroup | null } | { kind: "health-only"; group: ActionGroup };

const LINKAGES: CollectionConfig<McpServerLinkage, never> = {
  kind: "MCP linkage",
  key: (linkage) => linkage.serverId,
};

function followLinkages(
  handlers: CollectionWatchHandlers<CollectionFrame<McpServerLinkage, McpServerLinkageChunk, never>>
): () => void {
  return followMcpLinkages({ ...handlers, onFrame: ({ frame }) => handlers.onFrame(frame) });
}

const linkageChunk = (chunk: McpServerLinkageChunk): McpServerLinkage[] => chunk.linkages;

const GROUPS: CollectionConfig<ActionGroup, never> = { kind: "ActionGroup", key: (group) => group.key };

function followGroups(
  handlers: CollectionWatchHandlers<CollectionFrame<ActionGroup, ActionGroupChunk, never>>
): () => void {
  return followActionGroups({ ...handlers, onFrame: ({ frame }) => handlers.onFrame(frame) });
}

const groupChunk = (chunk: ActionGroupChunk): ActionGroup[] => chunk.actionGroups;

// /action-groups returns every configured group, sandbox-kind included; this page only cares
// about mcp-kind ones. Go catalog config validation requires every mcp-kind group whose config
// carries a server_id to have that
// server_id equal its own key -- so an oauth-linked group's key doubles as the join key, with no
// separate field needed.
function mergeRows(groups: ActionGroup[], linkages: McpServerLinkage[]): Row[] {
  const linkageBySid = new Map(linkages.map((linkage) => [linkage.serverId, linkage]));
  const matched = new Set<string>();
  const rows: Row[] = [];
  for (const group of groups) {
    if (group.executorKind !== ExecutorKind.MCP) continue;
    const linkage = linkageBySid.get(group.key);
    if (linkage) {
      matched.add(linkage.serverId);
      rows.push({ kind: "oauth", linkage, group });
    } else {
      rows.push({ kind: "health-only", group });
    }
  }
  for (const linkage of linkages) {
    if (!matched.has(linkage.serverId)) rows.push({ kind: "oauth", linkage, group: null });
  }
  return rows;
}

function rowKey(row: Row): string {
  return row.kind === "oauth" ? `linkage:${row.linkage.serverId}` : `group:${row.group.key}`;
}

function lifecycleColor(health: McpHealth): "green" | "orange" | "gray" {
  if (health == null) return "gray";
  switch (health.state) {
    case McpLifecycle.AVAILABLE:
      return "green";
    case McpLifecycle.DRAINING:
    case McpLifecycle.STOPPED:
      return health.reason === McpUnavailableReason.SUPERVISOR_STOPPED ? "orange" : "gray";
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
  const reason = mcpUnavailableReasonLabel(health?.reason);
  const state = health == null ? "pending" : mcpLifecycleLabel(health.state);
  return (
    <>
      <StateLabel>Connection</StateLabel>
      <Badge color={lifecycleColor(health)}>{reason ?? state}</Badge>
    </>
  );
}

function retryAtIso(value: Timestamp | undefined): string | null {
  if (value === undefined) return null;
  const json = toJson(TimestampSchema, value);
  if (typeof json !== "string") throw new Error("The Action Service returned an invalid refresh retry time.");
  return json;
}

function linkageStatus(status: McpLinkageStatus): { label: string; color: "green" | "orange" | "gray" } {
  switch (status) {
    case McpLinkageStatus.UNLINKED:
      return { label: "unlinked", color: "gray" };
    case McpLinkageStatus.LINKED:
      return { label: "linked", color: "green" };
    case McpLinkageStatus.DEGRADED:
      return { label: "degraded", color: "orange" };
    case McpLinkageStatus.EXPIRED:
      return { label: "expired", color: "orange" };
    default:
      return { label: `unknown (${status})`, color: "orange" };
  }
}

function LinkageState({ status }: { status: McpLinkageStatus }): JSX.Element {
  const presentation = linkageStatus(status);
  return (
    <>
      <StateLabel>OAuth link</StateLabel>
      <Badge color={presentation.color}>{presentation.label}</Badge>
    </>
  );
}

function RefreshFailure({ linkage }: { linkage: McpServerLinkage }): JSX.Element | null {
  const failure = linkage.refreshFailure;
  if (failure === undefined) return null;
  const retryAt = retryAtIso(failure.retryAt);
  const next =
    failure.action === McpRefreshAction.RECONNECT ? (
      "link the account again"
    ) : failure.action !== McpRefreshAction.RETRYING ? (
      `unknown refresh action (${failure.action})`
    ) : retryAt != null ? (
      <>
        retrying <TimeAgo value={retryAt} />
      </>
    ) : (
      "retrying"
    );
  return (
    <Text size="sm" c="orange.8" mt="xs" style={{ overflowWrap: "anywhere" }}>
      Token refresh failed {failure.attempts === 1 ? "once" : `${failure.attempts} times`}, {next}:
      <br />
      {failure.error}
    </Text>
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

export function McpServers(): JSX.Element {
  const linkages = useWatchCollection(LINKAGES, followLinkages, linkageChunk);
  const health = useWatchCollection(GROUPS, followGroups, groupChunk);
  const [busy, setBusy] = useState<{ serverId: string; operation: "link" | "disconnect" } | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function link(linkage: McpServerLinkage): Promise<void> {
    setBusy({ serverId: linkage.serverId, operation: "link" });
    setError(null);
    try {
      const authorizationUrl = await startMcpLinkage(linkage.serverId, linkage.scopes);
      window.location.assign(authorizationUrl);
    } catch (failure) {
      setError(displayableError(failure));
      setBusy(null);
    }
  }

  async function disconnect(linkage: McpServerLinkage): Promise<void> {
    setBusy({ serverId: linkage.serverId, operation: "disconnect" });
    setError(null);
    try {
      const updated = await disconnectMcpLinkage(linkage.serverId);
      linkages.replace(updated);
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setBusy(null);
    }
  }

  const loading = linkages.items === null || health.items === null;
  const rows = mergeRows(
    [...(health.items ?? [])].sort((left, right) => left.key.localeCompare(right.key)),
    linkages.items ?? []
  );

  return (
    <Stack>
      <Text c="dimmed" size="sm">
        Every configured MCP server, oauth-linked or not. Link one shared operator-managed OAuth account per
        oauth-linked server; Agents never receive these credentials.
      </Text>
      {linkages.connection.phase === "reconnecting" && (
        <Text role="status">OAuth linkage stream disconnected; showing last known state. Reconnecting…</Text>
      )}
      {health.connection.phase === "reconnecting" && (
        <Text role="status">MCP health stream disconnected; showing last known state. Reconnecting…</Text>
      )}
      {error && <Alert color="red">{error}</Alert>}
      {loading && <Text role="status">Loading MCP servers…</Text>}
      {rows.map((row) => (
        <Paper key={rowKey(row)} data-mcp-server={rowKey(row)} withBorder p="md">
          {row.kind === "oauth" ? (
            <>
              <Group justify="space-between" align="flex-start">
                <div>
                  <Text fw={600}>{row.linkage.serverId}</Text>
                  <Text size="sm" c="dimmed">
                    {row.linkage.serverUrl}
                  </Text>
                  <Text size="xs" c="dimmed">
                    Scopes: {row.linkage.scopes.length ? row.linkage.scopes.join(", ") : "provider default"}
                  </Text>
                </div>
                <States>
                  <LinkageState status={row.linkage.status} />
                  {row.group != null && row.linkage.status !== McpLinkageStatus.UNLINKED && (
                    <ConnectionState health={row.group.health} />
                  )}
                </States>
              </Group>
              <RefreshFailure linkage={row.linkage} />
              {row.group != null && <SchemaWarnings group={row.group} />}
              {/* A connection waiting on a linkage whose refresh failure is already shown has nothing to add. */}
              {row.group != null &&
                row.linkage.status !== McpLinkageStatus.UNLINKED &&
                !(
                  row.linkage.refreshFailure !== undefined &&
                  row.group.health?.reason === McpUnavailableReason.LINKAGE_UNAVAILABLE
                ) && <HealthDetail health={row.group.health} />}
              <Group justify="flex-end" mt="sm">
                <Button
                  loading={busy?.serverId === row.linkage.serverId && busy.operation === "link"}
                  disabled={loading || busy !== null}
                  onClick={() => void link(row.linkage)}
                >
                  {row.linkage.status === McpLinkageStatus.LINKED ? "Reconnect" : "Link account"}
                </Button>
                {row.linkage.status === McpLinkageStatus.LINKED && (
                  <Button
                    color="red"
                    variant="light"
                    loading={busy?.serverId === row.linkage.serverId && busy.operation === "disconnect"}
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
                    {row.group.executorDescription}
                  </Text>
                </div>
                <States>
                  <ConnectionState health={row.group.health} />
                </States>
              </Group>
              <HealthDetail health={row.group.health} />
              <SchemaWarnings group={row.group} />
            </>
          )}
        </Paper>
      ))}
      {!loading && rows.length === 0 && <Text c="dimmed">No MCP servers are configured.</Text>}
    </Stack>
  );
}
