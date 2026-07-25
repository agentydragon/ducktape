import { Badge, Button, Group, Loader, Stack, Text } from "@mantine/core";
import { useCallback, useEffect, useRef, useState } from "react";

import { shortDate } from "./approval_state.ts";
import {
  connectMcpOperatorAuth,
  connectOperatorConnection,
  disconnectMcpOperatorAuth,
  disconnectOperatorConnection,
  fetchDeploymentInfo,
  displayableError,
  listAgents,
  type AgentView,
  type DeploymentInfo,
  type OperatorConnectionName,
} from "./client.ts";
import { useConsoleEvents } from "./console_events.ts";
import { ExternalLink } from "./link.tsx";
import {
  getMcpServerStatus,
  listNodeDaemons,
  listMcpServers,
  type DaemonStatus,
  type McpOperatorAuthDegraded,
  type McpOperatorAuthStatus,
  type McpServerConnection,
  type McpServerProbe,
} from "./mcp_status_client.ts";
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

type McpServerView = {
  connection: McpServerConnection;
  probe: McpServerProbe | null;
  checking: boolean;
  error: string | null;
};

function isMcpOperatorAuthStatus(connection: McpServerConnection["connection"]): connection is McpOperatorAuthStatus {
  return connection !== null && "state" in connection;
}

function refreshFailureSummary({
  initial,
  latest,
  attempts,
  resolution,
}: McpOperatorAuthDegraded["refresh_failure"]): string {
  const failure =
    attempts > 1 && latest.message !== initial.message
      ? `${initial.message}; latest after ${attempts} attempts: ${latest.message}`
      : initial.message;
  return `${failure} · ${resolution}`;
}

function connectionSummary(server: McpServerConnection): string {
  const backend = server.backend.kind === "remote_mcp" ? "Remote MCP" : "In-process";
  const connection = server.connection;
  if (connection === null) {
    return server.backend.kind === "remote_mcp" && server.backend.auth.kind === "static_bearer"
      ? `${backend} · Console-managed credential`
      : `${backend} · no operator-linked account`;
  }
  if (isMcpOperatorAuthStatus(connection)) {
    if (connection.state.status === "unconnected") {
      return `${backend} · Not linked for ${connection.username}`;
    }
    const until = shortDate(
      typeof connection.state.token_expires_at === "string" ? connection.state.token_expires_at : null
    );
    return `${backend} · linked for ${connection.username}${until ? ` until ${until}` : ""}`;
  }
  if (connection.status === "unprovisioned") {
    return `${backend} · ${connection.display_name} OAuth client is not provisioned`;
  }
  if (connection.status === "unconnected") {
    return `${backend} · ${connection.display_name} is not connected`;
  }
  const until = shortDate(typeof connection.token_expires_at === "string" ? connection.token_expires_at : null);
  return `${backend} · ${connection.display_name} connected${until ? ` · token until ${until}` : ""}`;
}

function McpServerCard({
  view,
  onConnectMcp,
  onDisconnectMcp,
  onConnectProvider,
  onDisconnectProvider,
}: {
  view: McpServerView;
  onConnectMcp: (serverId: string) => void;
  onDisconnectMcp: (serverId: string) => void;
  onConnectProvider: (connection: OperatorConnectionName) => void;
  onDisconnectProvider: (connection: OperatorConnectionName) => void;
}) {
  const linkage = view.connection.connection;
  const linkedMcpServerId =
    linkage && "server_id" in linkage && typeof linkage.server_id === "string" ? linkage.server_id : null;
  const providerConnection =
    linkage && "connection" in linkage && typeof linkage.connection === "string"
      ? (linkage.connection as OperatorConnectionName)
      : null;
  const mcpState = isMcpOperatorAuthStatus(linkage) ? linkage.state : null;
  const providerState = linkage && !isMcpOperatorAuthStatus(linkage) ? linkage : null;
  const linkageStatus = mcpState?.status ?? providerState?.status;
  const unconnected = linkageStatus === "unconnected";
  const unprovisioned = linkageStatus === "unprovisioned";
  const degraded = linkageStatus === "degraded";
  const state = unprovisioned
    ? { label: "Unprovisioned", color: "orange" }
    : unconnected
      ? { label: "Unconnected", color: "gray" }
      : degraded || view.error || view.probe?.server.state.status === "degraded"
        ? { label: "Unavailable", color: "red" }
        : view.probe?.server.state.status === "alive"
          ? { label: "Available", color: "teal" }
          : { label: "Checking", color: "blue" };
  const reason =
    (providerState?.status === "unprovisioned" ? providerState.detail : null) ??
    (mcpState?.status === "degraded" ? refreshFailureSummary(mcpState.refresh_failure) : null) ??
    (providerState?.status === "degraded" ? refreshFailureSummary(providerState.refresh_failure) : null) ??
    view.error ??
    (view.probe?.server.state.status === "degraded" ? view.probe.server.state.degraded_reason : null);
  const connect = linkedMcpServerId
    ? () => onConnectMcp(linkedMcpServerId)
    : providerConnection
      ? () => onConnectProvider(providerConnection)
      : null;
  const disconnect = linkedMcpServerId
    ? () => onDisconnectMcp(linkedMcpServerId)
    : providerConnection
      ? () => onDisconnectProvider(providerConnection)
      : null;

  return (
    <section className="haku-shell-card">
      <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
        <Stack gap={2} style={{ minWidth: 0 }}>
          <Text fw={600}>{view.connection.server_id}</Text>
          <Text size="xs" c="dimmed">
            {connectionSummary(view.connection)}
          </Text>
          {reason && (
            <Text size="xs" c="red">
              {reason}
            </Text>
          )}
        </Stack>
        <Group gap={6} wrap="nowrap" style={{ flexShrink: 0 }}>
          {view.checking && view.probe && <Loader size={12} aria-label="Checking connection status" />}
          <Badge color={state.color} variant="light">
            {state.label}
          </Badge>
        </Group>
      </Group>
      {linkage && !unprovisioned && (
        <Group justify="flex-end" gap="xs" mt="sm">
          {linkageStatus === "connected" || linkageStatus === "degraded" ? (
            <Button size="compact-sm" variant="subtle" color="red" onClick={disconnect ?? undefined}>
              Disconnect
            </Button>
          ) : (
            <Button size="compact-sm" variant="light" onClick={connect ?? undefined}>
              Connect
            </Button>
          )}
        </Group>
      )}
    </section>
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

const AGENT_STATUS_COLOR: Record<AgentView["status"], string> = {
  draft: "blue",
  active: "teal",
  abandoned: "gray",
  disabled: "orange",
  deleted: "gray",
};

function AgentCard({ agent }: { agent: AgentView }) {
  const lastSeen = shortDate(agent.last_seen_at);
  const activated = shortDate(agent.activated_at);
  return (
    <section className="haku-shell-card">
      <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
        <Stack gap={2} style={{ minWidth: 0 }}>
          <Text fw={600}>{agent.display_name}</Text>
          <Text size="xs" c="dimmed">
            {lastSeen ? `Last seen ${lastSeen}` : "Not seen yet"}
          </Text>
          <Text size="xs" c="dimmed">
            {agent.credential_kind === "oauth" ? "OAuth" : "Static credential"}
            {activated ? ` · active since ${activated}` : ""}
          </Text>
        </Stack>
        <Badge color={AGENT_STATUS_COLOR[agent.status]} variant="light">
          {agent.status}
        </Badge>
      </Group>
    </section>
  );
}

// Operator settings — the console's rarely-touched MCP account linkage, rendered as one of the
// shell chrome's mutually exclusive panels.
export function SettingsPanel() {
  const [agents, setAgents] = useState<AgentView[] | null>(null);
  const [mcpServers, setMcpServers] = useState<McpServerView[] | null>(null);
  const [deployment, setDeployment] = useState<DeploymentInfo | null>(null);
  const [daemons, setDaemons] = useState<DaemonStatus[] | null>(null);
  const [mcpError, setMcpError] = useState<string | null>(null);
  const [agentsError, setAgentsError] = useState<string | null>(null);
  const [deploymentError, setDeploymentError] = useState<string | null>(null);
  const [daemonsError, setDaemonsError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const loadGeneration = useRef(0);
  const daemonGeneration = useRef(0);
  const versions = deployment ? deploymentVersions(deployment) : [];

  const load = useCallback(() => {
    const generation = ++loadGeneration.current;
    setLoading(true);
    const agentsRequest = listAgents().then(
      (nextAgents) => {
        if (generation !== loadGeneration.current) return;
        setAgents(nextAgents);
        setAgentsError(null);
      },
      (e: unknown) => {
        if (generation !== loadGeneration.current) return;
        setAgentsError(displayableError(e));
      }
    );
    const mcpRequest = listMcpServers().then(
      async (connections) => {
        if (generation !== loadGeneration.current) return;
        setMcpError(null);
        setMcpServers((current) => {
          const previous = new Map(current?.map((view) => [view.connection.server_id, view]));
          return connections.map((connection) => ({
            connection,
            probe: previous.get(connection.server_id)?.probe ?? null,
            checking: true,
            error: null,
          }));
        });
        await Promise.all(
          connections.map(async (connection) => {
            try {
              const probe = await getMcpServerStatus(connection.server_id);
              if (generation !== loadGeneration.current) return;
              setMcpServers(
                (current) =>
                  current?.map((view) =>
                    view.connection.server_id === connection.server_id
                      ? { connection: probe.connection, probe, checking: false, error: null }
                      : view
                  ) ?? null
              );
            } catch (e) {
              if (generation !== loadGeneration.current) return;
              const error = e instanceof Error ? e.message : String(e);
              setMcpServers(
                (current) =>
                  current?.map((view) =>
                    view.connection.server_id === connection.server_id ? { ...view, checking: false, error } : view
                  ) ?? null
              );
            }
          })
        );
      },
      (e: unknown) => {
        if (generation !== loadGeneration.current) return;
        setMcpError(displayableError(e));
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
        setDeploymentError(displayableError(e));
      }
    );
    void Promise.all([agentsRequest, mcpRequest, deploymentRequest]).then(() => {
      if (generation === loadGeneration.current) setLoading(false);
    });
  }, []);

  const loadDaemons = useCallback(() => {
    const generation = ++daemonGeneration.current;
    void listNodeDaemons().then(
      (nextDaemons) => {
        if (generation !== daemonGeneration.current) return;
        setDaemons(nextDaemons);
        setDaemonsError(null);
      },
      (e: unknown) => {
        if (generation !== daemonGeneration.current) return;
        setDaemonsError(displayableError(e));
      }
    );
  }, []);

  useEffect(() => {
    loadDaemons();
    const interval = window.setInterval(loadDaemons, 10_000);
    return () => {
      window.clearInterval(interval);
      loadGeneration.current += 1;
      daemonGeneration.current += 1;
    };
  }, [loadDaemons]);

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
    <section className="haku-page" aria-label="Settings">
      <header className="haku-page-header">
        <div className="haku-page-bar">
          <Text fw={700}>Settings</Text>
          <Button size="xs" variant="light" color="gray" loading={loading} onClick={() => load()}>
            Refresh
          </Button>
        </div>
      </header>
      <div className="haku-page-scroll">
        <Stack gap="xs" className="haku-page-list">
          <SectionHeading
            title="Agents"
            description="Clients authorized to use Haku. Activity is historical and does not indicate that a client is currently online."
          />
          {agentsError && (
            <Text c="red" size="sm">
              Failed to load Agents: {agentsError}
            </Text>
          )}
          {!agents && !agentsError && (
            <Group justify="center" p="xl">
              <Loader aria-label="Loading Agents" />
            </Group>
          )}
          {agents && agents.length === 0 && (
            <section className="haku-shell-card">
              <Text size="sm" c="dimmed">
                No Agents have been authorized.
              </Text>
            </section>
          )}
          {agents?.map((agent) => (
            <AgentCard key={agent.agent_id} agent={agent} />
          ))}
          <SectionHeading
            title="MCP servers"
            description="Live availability through the console's MCP reflection tools. Status refreshes automatically and may verify linked credentials."
          />
          {mcpError && (
            <Text c="red" size="sm">
              Failed to load MCP servers: {mcpError}
            </Text>
          )}
          {!mcpServers && !mcpError && (
            <Group justify="center" p="xl">
              <Loader aria-label="Loading MCP servers" />
            </Group>
          )}
          {mcpServers && mcpServers.length === 0 && (
            <section className="haku-shell-card">
              <Text size="sm" c="dimmed">
                No MCP servers are configured.
              </Text>
            </section>
          )}
          {mcpServers?.map((view) => (
            <McpServerCard
              key={view.connection.server_id}
              view={view}
              onConnectMcp={connect}
              onDisconnectMcp={disconnect}
              onConnectProvider={connectProvider}
              onDisconnectProvider={disconnectProvider}
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
    </section>
  );
}
