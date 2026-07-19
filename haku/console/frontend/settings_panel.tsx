import { Badge, Button, Group, Loader, Stack, Text } from "@mantine/core";
import { useCallback, useEffect, useRef, useState } from "react";

import { shortDate } from "./approval_state.ts";
import {
  connectMcpOperatorAuth,
  connectOperatorConnection,
  disconnectMcpOperatorAuth,
  disconnectOperatorConnection,
  fetchDeploymentInfo,
  fetchMcpOperatorAuthStatuses,
  fetchNodeDaemons,
  fetchOperatorConnections,
  type DeploymentInfo,
  type DaemonStatus,
  type McpOperatorAuthStatus,
  type OperatorConnectionName,
  type ProviderConnectionStatus,
} from "./client.ts";
import { useConsoleEvents } from "./console_events.ts";
import { ExternalLink } from "./link.tsx";
import { openExternal, POPUP_HINT } from "./open_external.ts";
import { toastError, toastSuccess } from "./toast.ts";

type DeploymentVersion = {
  label: string;
  image: DeploymentInfo["server"];
};

export function deploymentVersions(deployment: DeploymentInfo): DeploymentVersion[] {
  const { server, frontend } = deployment;
  if (server.source_commit && server.source_commit === frontend.source_commit) {
    return [{ label: "Deployed", image: server }];
  }
  return [
    ...(server.source_commit ? [{ label: "Server", image: server }] : []),
    ...(frontend.source_commit ? [{ label: "Web", image: frontend }] : []),
  ];
}

function VersionLink({ version }: { version: DeploymentVersion }) {
  const commit = version.image.source_commit?.slice(0, 12);
  if (!commit) return null;
  const content = version.image.source_commit_url ? (
    <ExternalLink href={version.image.source_commit_url} size="xs" ff="monospace">
      {commit}
    </ExternalLink>
  ) : (
    <Text size="xs" ff="monospace">
      {commit}
    </Text>
  );
  return (
    <Group gap="xs" wrap="nowrap" title={version.image.image_tag ?? version.label}>
      <Text size="xs" c="dimmed">
        {version.label}
      </Text>
      {content}
    </Group>
  );
}

// A plain (un-boxed) section label + blurb over the boxed cards it introduces.
function SectionHeading({ title, description }: { title: string; description: string }) {
  return (
    <div>
      <Text fw={600}>{title}</Text>
      <Text size="xs" c="dimmed" mt={4}>
        {description}
      </Text>
    </div>
  );
}

// Shared presentational card for an operator connection (MCP account or provider). Takes only
// primitives so each caller does its own discriminated-union narrowing to compute the strings.
function ConnectionCard({
  title,
  subtitle,
  connected,
  onConnect,
  onDisconnect,
}: {
  title: string;
  subtitle: string;
  connected: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
}) {
  return (
    <section className="haku-shell-card">
      <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
        <Stack gap={2} style={{ minWidth: 0 }}>
          <Text fw={600}>{title}</Text>
          <Text size="xs" c="dimmed">
            {subtitle}
          </Text>
        </Stack>
        <Badge color={connected ? "teal" : "gray"} variant="light">
          {connected ? "Connected" : "Unconnected"}
        </Badge>
      </Group>
      <Group justify="flex-end" gap="xs" mt="sm">
        {connected ? (
          <Button size="compact-sm" variant="subtle" color="red" onClick={onDisconnect}>
            Disconnect
          </Button>
        ) : (
          <Button size="compact-sm" variant="light" onClick={onConnect}>
            Connect
          </Button>
        )}
      </Group>
    </section>
  );
}

function McpAccountCard({
  status,
  onConnect,
  onDisconnect,
}: {
  status: McpOperatorAuthStatus;
  onConnect: () => void;
  onDisconnect: () => void;
}) {
  const connected = status.status === "connected";
  const until = status.status === "connected" ? shortDate(status.token_expires_at) : null;
  return (
    <ConnectionCard
      title={status.server_id}
      subtitle={
        connected
          ? `Linked for ${status.username}${until ? ` until ${until}` : ""}`
          : `Not linked for ${status.username}`
      }
      connected={connected}
      onConnect={onConnect}
      onDisconnect={onDisconnect}
    />
  );
}

function ProviderConnectionCard({
  status,
  onConnect,
  onDisconnect,
}: {
  status: ProviderConnectionStatus;
  onConnect: () => void;
  onDisconnect: () => void;
}) {
  const connected = status.status === "connected";
  const until = status.status === "connected" ? shortDate(status.token_expires_at) : null;
  return (
    <ConnectionCard
      title={status.display_name}
      subtitle={connected ? `Connected${until ? ` · token until ${until}` : ""}` : "Not connected"}
      connected={connected}
      onConnect={onConnect}
      onDisconnect={onDisconnect}
    />
  );
}

const DAEMON_STATUS_COLOR: Record<DaemonStatus["status"], string> = {
  connected: "teal",
  busy: "blue",
  stale: "yellow",
  offline: "gray",
};

function DaemonCard({ daemon }: { daemon: DaemonStatus }) {
  const seen = daemon.last_heartbeat_at ? shortDate(daemon.last_heartbeat_at) : null;
  return (
    <section className="haku-shell-card">
      <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
        <Stack gap={2} style={{ minWidth: 0 }}>
          <Text fw={600}>{daemon.display_name}</Text>
          <Text size="xs" c="dimmed">
            {daemon.version ? `hostexecd ${daemon.version}` : "Never connected"}
            {seen ? ` · heartbeat ${seen}` : ""}
          </Text>
          {daemon.active_execution_id && (
            <Text size="xs" c="dimmed" ff="monospace">
              {daemon.active_execution_id}
            </Text>
          )}
        </Stack>
        <Badge color={DAEMON_STATUS_COLOR[daemon.status]} variant="light">
          {daemon.status}
        </Badge>
      </Group>
    </section>
  );
}

// Operator settings — the console's rarely-touched MCP account linkage, rendered as one of the
// shell chrome's mutually exclusive panels.
export function SettingsPanel() {
  const [statuses, setStatuses] = useState<McpOperatorAuthStatus[] | null>(null);
  const [providerStatuses, setProviderStatuses] = useState<ProviderConnectionStatus[] | null>(null);
  const [deployment, setDeployment] = useState<DeploymentInfo | null>(null);
  const [daemons, setDaemons] = useState<DaemonStatus[] | null>(null);
  const [statusesError, setStatusesError] = useState<string | null>(null);
  const [providerStatusesError, setProviderStatusesError] = useState<string | null>(null);
  const [deploymentError, setDeploymentError] = useState<string | null>(null);
  const [daemonsError, setDaemonsError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const loadGeneration = useRef(0);
  const versions = deployment ? deploymentVersions(deployment) : [];

  const load = useCallback(() => {
    const generation = ++loadGeneration.current;
    setLoading(true);
    const statusesRequest = fetchMcpOperatorAuthStatuses().then(
      (nextStatuses) => {
        if (generation !== loadGeneration.current) return;
        setStatuses(nextStatuses);
        setStatusesError(null);
      },
      (e: unknown) => {
        if (generation !== loadGeneration.current) return;
        setStatusesError(e instanceof Error ? e.message : String(e));
      }
    );
    const providerRequest = fetchOperatorConnections().then(
      (nextProviderStatuses) => {
        if (generation !== loadGeneration.current) return;
        setProviderStatuses(nextProviderStatuses);
        setProviderStatusesError(null);
      },
      (e: unknown) => {
        if (generation !== loadGeneration.current) return;
        setProviderStatusesError(e instanceof Error ? e.message : String(e));
      }
    );
    const deploymentRequest = fetchDeploymentInfo().then(
      (nextDeployment) => {
        if (generation !== loadGeneration.current) return;
        setDeployment(nextDeployment);
        setDeploymentError(null);
      },
      (e: unknown) => {
        if (generation !== loadGeneration.current) return;
        setDeploymentError(e instanceof Error ? e.message : String(e));
      }
    );
    const daemonsRequest = fetchNodeDaemons().then(
      (nextDaemons) => {
        if (generation !== loadGeneration.current) return;
        setDaemons(nextDaemons);
        setDaemonsError(null);
      },
      (e: unknown) => {
        if (generation !== loadGeneration.current) return;
        setDaemonsError(e instanceof Error ? e.message : String(e));
      }
    );
    void Promise.all([statusesRequest, providerRequest, deploymentRequest, daemonsRequest]).then(() => {
      if (generation === loadGeneration.current) setLoading(false);
    });
  }, []);

  useEffect(() => {
    load();
    const interval = window.setInterval(load, 10_000);
    return () => {
      window.clearInterval(interval);
      loadGeneration.current += 1;
    };
  }, [load]);

  useConsoleEvents((event) => {
    if (
      event.event_type === "sync" ||
      event.event_type === "mcp_operator_auth_changed" ||
      event.event_type === "operator_connection_changed"
    )
      load();
  });

  function connect(serverId: string) {
    connectMcpOperatorAuth(serverId).then(
      (started) => {
        if (!openExternal(started.authorization_url)) {
          toastError("Pop-up blocked", POPUP_HINT);
          return;
        }
        toastSuccess("MCP account link started", "Finish the authorization in the new tab.");
      },
      (e: unknown) => toastError("Couldn't start MCP account link", e)
    );
  }

  function disconnect(serverId: string) {
    disconnectMcpOperatorAuth(serverId).then(
      () => {
        toastSuccess("MCP account disconnected", serverId);
      },
      (e: unknown) => toastError("Couldn't disconnect MCP account", e)
    );
  }

  function connectProvider(connection: OperatorConnectionName) {
    connectOperatorConnection(connection).then(
      (started) => {
        if (!openExternal(started.authorization_url)) {
          toastError("Pop-up blocked", POPUP_HINT);
          return;
        }
        toastSuccess("Account connection started", "Finish the authorization in the new tab.");
      },
      (e: unknown) => toastError("Couldn't start account connection", e)
    );
  }

  function disconnectProvider(connection: OperatorConnectionName) {
    disconnectOperatorConnection(connection).then(
      (status) => toastSuccess("Account disconnected", status.display_name),
      (e: unknown) => toastError("Couldn't disconnect account", e)
    );
  }

  return (
    <aside className="haku-shell-card haku-shell-settings" aria-label="Settings">
      <Group justify="space-between" align="center" wrap="nowrap" className="haku-shell-header">
        <Text fw={700}>Settings</Text>
        <Button size="compact-xs" variant="light" color="gray" loading={loading} onClick={() => load()}>
          Refresh
        </Button>
      </Group>
      <div className="haku-shell-scroll">
        <Stack gap="xs">
          <SectionHeading
            title="MCP accounts"
            description="Operator OAuth links for connected MCP servers. Connect one to let the console execute its tools with your account."
          />
          {statusesError && (
            <Text c="red" size="sm">
              Failed to load MCP accounts: {statusesError}
            </Text>
          )}
          {!statuses && !statusesError && (
            <Group justify="center" p="xl">
              <Loader />
            </Group>
          )}
          {statuses && statuses.length === 0 && (
            <section className="haku-shell-card">
              <Text size="sm" c="dimmed">
                No operator-linked MCP servers are configured.
              </Text>
            </section>
          )}
          {statuses?.map((status) => (
            <McpAccountCard
              key={status.server_id}
              status={status}
              onConnect={() => connect(status.server_id)}
              onDisconnect={() => disconnect(status.server_id)}
            />
          ))}
          <SectionHeading
            title="Connected accounts"
            description="External accounts used by in-process tools. Each linkage requests and stores only its configured scopes."
          />
          {providerStatusesError && (
            <Text c="red" size="sm">
              Failed to load connected accounts: {providerStatusesError}
            </Text>
          )}
          {providerStatuses?.map((status) => (
            <ProviderConnectionCard
              key={status.connection}
              status={status}
              onConnect={() => connectProvider(status.connection)}
              onDisconnect={() => disconnectProvider(status.connection)}
            />
          ))}
          <SectionHeading
            title="Node daemons"
            description="Outbound execution daemons. Heartbeats determine whether approved node work can be dispatched."
          />
          {daemonsError && (
            <Text c="red" size="sm">
              Failed to load node daemons: {daemonsError}
            </Text>
          )}
          {daemons?.map((daemon) => (
            <DaemonCard key={daemon.daemon_id} daemon={daemon} />
          ))}
          {deployment && (
            <div>
              <Text fw={600}>Version</Text>
              <Stack gap={2} mt={4}>
                {versions.map((version) => (
                  <VersionLink key={version.label} version={version} />
                ))}
                {versions.length === 0 && (
                  <Text size="xs" c="dimmed">
                    Deployment metadata unavailable.
                  </Text>
                )}
              </Stack>
            </div>
          )}
          {deploymentError && (
            <Text c="red" size="sm">
              Failed to load deployment version: {deploymentError}
            </Text>
          )}
        </Stack>
      </div>
    </aside>
  );
}
