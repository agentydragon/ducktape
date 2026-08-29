import { Badge, Button, Group, Loader, Select, Stack, Tabs, Text } from "@mantine/core";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { shortDate } from "./approval_state";
import { useAsyncResource, type AsyncResource, type AsyncResourceLoader } from "./async_resource";
import {
  connectMcpOperatorAuth,
  connectOperatorConnection,
  disconnectMcpOperatorAuth,
  disconnectOperatorConnection,
  fetchDeploymentInfo,
  displayableError,
  listAgents,
  updateAgentAccessProfile,
  type AgentView,
  type DeploymentInfo,
  type OperatorConnectionName,
} from "./client";
import { useConsoleEvents } from "./console_events";
import { GrantsPanel } from "./grants_panel";
import { ExternalLink } from "./link";
import { usePushNotifications, type PushState } from "./push_subscription";
import {
  getIndexStatus,
  getMcpServerStatus,
  listNodeDaemons,
  listMcpServers,
  type DaemonStatus,
  type IndexState,
  type McpOperatorAuthDegraded,
  type McpOperatorAuthStatus,
  type McpServerConnection,
  type McpServerProbe,
} from "./mcp_status_client";
import { openExternal, POPUP_HINT } from "./open_external";
import { toastError, toastSuccess } from "./toast";

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

function ResourcePanel<T>({
  title,
  description,
  resource,
  label,
  emptyMessage,
  isEmpty,
  children,
}: {
  title?: string;
  description?: string;
  resource: AsyncResource<T>;
  label: string;
  emptyMessage?: string;
  isEmpty?: (data: T) => boolean;
  children: (data: T) => ReactNode;
}) {
  const { data, error } = resource;
  const errorNotice =
    error && data ? (
      <Text c="red" size="sm">
        Failed to refresh {label}: {error}
      </Text>
    ) : null;
  const body =
    !data && error ? (
      <Text c="red" size="sm">
        Failed to load {label}: {error}
      </Text>
    ) : !data ? (
      <Group justify="center" p="xl">
        <Loader aria-label={`Loading ${label}`} />
      </Group>
    ) : emptyMessage && (isEmpty ? isEmpty(data) : Array.isArray(data) && data.length === 0) ? (
      <section className="haku-shell-card">
        <Text size="sm" c="dimmed">
          {emptyMessage}
        </Text>
      </section>
    ) : (
      children(data)
    );
  return title ? (
    <Stack gap="xs" className="haku-page-list">
      <SectionHeading title={title} description={description ?? ""} />
      {errorNotice}
      {body}
    </Stack>
  ) : (
    <>
      {errorNotice}
      {body}
    </>
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
    const linkedUntil = shortDate(
      typeof connection.state.token_expires_at === "string" ? connection.state.token_expires_at : null
    );
    switch (connection.state.status) {
      case "unconnected":
        return `${backend} · Not linked for ${connection.username}`;
      case "degraded":
        return `${backend} · linked for ${connection.username} · refresh failing${linkedUntil ? ` · token until ${linkedUntil}` : ""}`;
      case "connected":
        return `${backend} · linked for ${connection.username}${linkedUntil ? ` until ${linkedUntil}` : ""}`;
    }
  }
  const until = shortDate(typeof connection.token_expires_at === "string" ? connection.token_expires_at : null);
  switch (connection.status) {
    case "unprovisioned":
      return `${backend} · ${connection.display_name} OAuth client is not provisioned`;
    case "unconnected":
      return `${backend} · ${connection.display_name} is not connected`;
    case "degraded":
      return `${backend} · ${connection.display_name} refresh failing${until ? ` · token until ${until}` : ""}`;
    case "connected":
      return `${backend} · ${connection.display_name} connected${until ? ` · token until ${until}` : ""}`;
  }
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

function AgentCard({
  agent,
  accessProfiles,
  saving,
  onAccessProfileChange,
}: {
  agent: AgentView;
  accessProfiles: string[];
  saving: boolean;
  onAccessProfileChange: (agent: AgentView, accessProfileId: string) => void;
}) {
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
      <Select
        mt="sm"
        label="Access profile"
        description={
          agent.credential_kind === "static"
            ? "Managed by deployment configuration."
            : agent.access_profile_id === null
              ? "This preexisting Agent has no assignment; all tool calls require approval until you choose one."
              : "Tool calls outside this profile still require your approval."
        }
        data={accessProfiles.map((profile) => ({
          value: profile,
          label: profile.replaceAll("_", " "),
        }))}
        value={agent.access_profile_id}
        placeholder="Select an access profile"
        onChange={(profile) => {
          if (profile && profile !== agent.access_profile_id) onAccessProfileChange(agent, profile);
        }}
        allowDeselect={false}
        disabled={saving || agent.credential_kind === "static"}
      />
    </section>
  );
}

const PUSH_STATE_DISPLAY: Record<PushState["status"], { label: string; color: string; description: string }> = {
  on: { label: "On", color: "teal", description: "This browser will be notified about pending tool calls." },
  off: { label: "Off", color: "gray", description: "This browser will not be notified." },
  busy: { label: "…", color: "gray", description: "Checking this browser's notification state." },
  denied: {
    label: "Blocked",
    color: "orange",
    description: "This browser blocked notifications for the console. Re-allow them in its site settings.",
  },
  unsupported: {
    label: "Unsupported",
    color: "gray",
    description: "This browser does not support Web Push notifications.",
  },
  disabled: {
    label: "Unavailable",
    color: "gray",
    description: "This console has no push key configured, so it cannot send notifications.",
  },
  failed: { label: "Error", color: "red", description: "Could not read this browser's notification state." },
};

function PushNotificationCard() {
  const { state, devices, enable, disable, forget } = usePushNotifications();
  const display = PUSH_STATE_DISPLAY[state.status];
  const actionable = state.status === "on" || state.status === "off" || state.status === "failed";
  const thisEndpoint = state.status === "on" ? state.endpoint : null;
  const others = devices.filter((device) => device.endpoint !== thisEndpoint);
  return (
    <>
      <section className="haku-shell-card">
        <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
          <Stack gap={2} style={{ minWidth: 0 }}>
            <Text fw={600}>This browser</Text>
            <Text size="xs" c="dimmed">
              {state.status === "failed" ? state.message : display.description}
            </Text>
          </Stack>
          <Badge color={display.color} variant="light">
            {display.label}
          </Badge>
        </Group>
        {actionable && (
          <Group justify="flex-end" gap="xs" mt="sm">
            {state.status === "on" ? (
              <Button size="compact-sm" variant="subtle" color="red" onClick={() => void disable()}>
                Turn off
              </Button>
            ) : (
              <Button size="compact-sm" variant="light" onClick={() => void enable()}>
                Turn on
              </Button>
            )}
          </Group>
        )}
      </section>
      {others.map((device) => (
        <section className="haku-shell-card" key={device.endpoint}>
          <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
            <Stack gap={2} style={{ minWidth: 0 }}>
              <Text fw={600}>{device.userAgent ?? "Unidentified browser"}</Text>
              <Text size="xs" c="dimmed">
                Also notified{shortDate(device.createdAt) ? ` · added ${shortDate(device.createdAt)}` : ""}
              </Text>
            </Stack>
            <Button size="compact-sm" variant="subtle" color="red" onClick={() => void forget(device.endpoint)}>
              Forget
            </Button>
          </Group>
        </section>
      ))}
    </>
  );
}

const SETTINGS_TABS = ["mcp", "agents", "grants", "notifications", "nodes", "system"] as const;
type SettingsTab = (typeof SETTINGS_TABS)[number];

export function settingsTabFromSearch(search: string): SettingsTab {
  const requested = new URLSearchParams(search).get("tab");
  return SETTINGS_TABS.find((tab) => tab === requested) ?? "mcp";
}

function settingsTabFromLocation(): SettingsTab {
  return settingsTabFromSearch(window.location.search);
}

function SystemStatusCard({ deployment }: { deployment: DeploymentInfo }) {
  const versions = deploymentVersions(deployment);
  const serverCommit = deployment.server.source_commit;
  const frontendCommit = deployment.frontend.source_commit;
  const inSync = Boolean(serverCommit && frontendCommit && serverCommit === frontendCommit);
  const mixed = Boolean(serverCommit && frontendCommit && serverCommit !== frontendCommit);
  const status = inSync
    ? { label: "In sync", color: "teal", description: "The server and web application run the same revision." }
    : mixed
      ? {
          label: "Mixed revisions",
          color: "orange",
          description: "The deployment is still converging on one revision.",
        }
      : { label: "Unknown", color: "gray", description: "Complete deployment revision metadata is unavailable." };

  return (
    <section className="haku-shell-card">
      <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
        <Stack gap={2} style={{ minWidth: 0 }}>
          <Text fw={600}>System status</Text>
          <Text size="xs" c="dimmed">
            {status.description}
          </Text>
        </Stack>
        <Badge color={status.color} variant="light">
          {status.label}
        </Badge>
      </Group>
      <Stack gap={4} mt="sm">
        {versions.map((version) => (
          <VersionLink key={version.label} version={version} />
        ))}
        {versions.length === 0 && (
          <Text size="xs" c="dimmed">
            Deployment metadata unavailable.
          </Text>
        )}
      </Stack>
    </section>
  );
}

type IndexDisplay = { label: string; color: string; description: string };

export function indexStatusDisplay(index: IndexState): IndexDisplay {
  if (index.index_type === "git") {
    if (index.remote_commit && index.indexed_commit === index.remote_commit) {
      return { label: "Current", color: "teal", description: "Indexed at the latest remote commit." };
    }
    if (!index.indexed_commit && index.remote_commit) {
      return { label: "Not indexed", color: "orange", description: "The first index build is still pending." };
    }
    if (index.indexed_commit && index.remote_commit) {
      return { label: "Behind", color: "orange", description: "A newer remote commit is waiting to be indexed." };
    }
    return { label: "Unknown", color: "gray", description: "The remote revision has not been observed yet." };
  }
  if (index.stale_sessions === 0 && index.unindexed_messages === 0) {
    return { label: "Current", color: "teal", description: "All completed chat messages are indexed." };
  }
  return { label: "Catching up", color: "orange", description: "New or changed chat messages are waiting." };
}

function commitLabel(commit: string | null | undefined): string {
  return commit?.slice(0, 12) ?? "none";
}

function IndexStatusCard({ index }: { index: IndexState }) {
  const status = indexStatusDisplay(index);
  const indexedAt = shortDate((index.index_type === "git" ? index.indexed_at : index.last_indexed_at) ?? null);
  return (
    <section className="haku-shell-card">
      <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
        <Stack gap={2} style={{ minWidth: 0 }}>
          <Text fw={600}>{index.index_id}</Text>
          <Text size="xs" c="dimmed">
            {status.description}
          </Text>
        </Stack>
        <Badge color={status.color} variant="light">
          {status.label}
        </Badge>
      </Group>
      <Stack gap={4} mt="sm">
        {index.index_type === "git" ? (
          <>
            <Text size="xs" c="dimmed">
              {index.branch ?? "Git"} · {index.files ?? 0} files · {index.chunks ?? 0} chunks ·{" "}
              {index.embedded_chunks ?? 0} embedded
              {(index.pending_chunks ?? 0) > 0 ? ` · ${index.pending_chunks} pending` : ""}
            </Text>
            <Text size="xs" c="dimmed" ff="monospace">
              indexed {commitLabel(index.indexed_commit)}
              {index.remote_commit !== index.indexed_commit ? ` · remote ${commitLabel(index.remote_commit)}` : ""}
            </Text>
          </>
        ) : (
          <>
            <Text size="xs" c="dimmed">
              {index.sessions} sessions · {index.chunks} chunks · {index.embedded_chunks} embedded
              {index.pending_chunks > 0 ? ` · ${index.pending_chunks} pending` : ""}
            </Text>
            {(index.stale_sessions > 0 || index.unindexed_messages > 0) && (
              <Text size="xs" c="dimmed">
                {index.stale_sessions} stale sessions · {index.unindexed_messages} messages pending
              </Text>
            )}
          </>
        )}
        <Text size="xs" c="dimmed">
          {indexedAt ? `Last indexed ${indexedAt}` : "Not indexed yet"}
          {(index.superseded_chunks ?? 0) > 0 ? ` · ${index.superseded_chunks} superseded chunks` : ""}
        </Text>
      </Stack>
    </section>
  );
}

export function SettingsPanel(): JSX.Element {
  const [activeTab, setActiveTab] = useState<SettingsTab>(settingsTabFromLocation);
  const [savingAgentId, setSavingAgentId] = useState<string | null>(null);
  const loadMcpServers = useCallback<AsyncResourceLoader<McpServerView[]>>(async (emit, previous) => {
    const connections = await listMcpServers();
    const previousViews = new Map(previous?.map((view) => [view.connection.server_id, view]));
    let views: McpServerView[] = connections.map((connection) => ({
      connection,
      probe: previousViews.get(connection.server_id)?.probe ?? null,
      checking: true,
      error: null,
    }));
    emit(views);
    await Promise.all(
      connections.map(async (connection) => {
        try {
          const probe = await getMcpServerStatus(connection.server_id);
          views = views.map((view) =>
            view.connection.server_id === connection.server_id
              ? { connection: probe.connection, probe, checking: false, error: null }
              : view
          );
        } catch (e) {
          const error = e instanceof Error ? e.message : String(e);
          views = views.map((view) =>
            view.connection.server_id === connection.server_id ? { ...view, checking: false, error } : view
          );
        }
        emit(views);
      })
    );
    return views;
  }, []);
  const resourceOptions = (tab: SettingsTab, pollMs?: number) => ({
    enabled: activeTab === tab,
    pollMs,
    formatError: displayableError,
  });
  const mcpResource = useAsyncResource(loadMcpServers, resourceOptions("mcp"));
  const agentsResource = useAsyncResource(listAgents, resourceOptions("agents"));
  const deploymentResource = useAsyncResource(fetchDeploymentInfo, resourceOptions("system"));
  const indexStatusResource = useAsyncResource(getIndexStatus, resourceOptions("system"));
  const daemonsResource = useAsyncResource(listNodeDaemons, resourceOptions("nodes", 10_000));
  const refreshMcp = mcpResource.refresh;
  const refreshAgents = agentsResource.refresh;
  const refreshDeployment = deploymentResource.refresh;
  const refreshIndexStatus = indexStatusResource.refresh;
  const refreshDaemons = daemonsResource.refresh;
  const agentAccessProfiles = agentsResource.data?.access_profiles ?? [];
  const refreshActiveTab = useCallback(() => {
    if (activeTab === "mcp") return refreshMcp();
    if (activeTab === "agents") return refreshAgents();
    if (activeTab === "nodes") return refreshDaemons();
    if (activeTab === "system") {
      refreshDeployment();
      refreshIndexStatus();
    }
  }, [activeTab, refreshAgents, refreshDaemons, refreshDeployment, refreshIndexStatus, refreshMcp]);
  useEffect(() => {
    const restoreTab = () => setActiveTab(settingsTabFromLocation());
    window.addEventListener("popstate", restoreTab);
    return () => {
      window.removeEventListener("popstate", restoreTab);
    };
  }, []);
  useConsoleEvents((event) => {
    if (event.event_type === "sync") refreshActiveTab();
    if (
      activeTab === "mcp" &&
      (event.event_type === "mcp_operator_auth_changed" || event.event_type === "operator_connection_changed")
    )
      refreshMcp();
  });
  function selectTab(value: string | null) {
    if (!value || !SETTINGS_TABS.includes(value as SettingsTab)) return;
    const tab = value as SettingsTab;
    setActiveTab(tab);
    const url = new URL(window.location.href);
    if (tab === "mcp") url.searchParams.delete("tab");
    else url.searchParams.set("tab", tab);
    window.history.replaceState(null, "", url);
  }
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
        refreshMcp();
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
      (status) => {
        toastSuccess("Account disconnected", status.display_name);
        refreshMcp();
      },
      (e: unknown) => toastError("Couldn't disconnect account", e)
    );
  }
  function changeAgentAccessProfile(agent: AgentView, accessProfileId: string) {
    setSavingAgentId(agent.agent_id);
    updateAgentAccessProfile(agent.agent_id, accessProfileId).then(
      (updated) => {
        agentsResource.update((current) =>
          current
            ? {
                ...current,
                agents: current.agents.map((item) => (item.agent_id === updated.agent_id ? updated : item)),
              }
            : null
        );
        setSavingAgentId(null);
        toastSuccess(
          "Agent access profile updated",
          `${updated.display_name} now uses ${updated.access_profile_id?.replaceAll("_", " ")}.`
        );
      },
      (e: unknown) => {
        setSavingAgentId(null);
        toastError("Couldn't update Agent access profile", e);
      }
    );
  }

  const loading =
    activeTab === "mcp"
      ? mcpResource.loading
      : activeTab === "agents"
        ? agentsResource.loading
        : activeTab === "nodes"
          ? daemonsResource.loading
          : activeTab === "system"
            ? deploymentResource.loading || indexStatusResource.loading
            : false;

  return (
    <Tabs
      value={activeTab}
      onChange={selectTab}
      keepMounted={false}
      color="haku"
      className="haku-page"
      aria-label="Settings"
    >
      <header className="haku-page-header">
        <div className="haku-page-bar">
          <Text fw={700}>Settings</Text>
          {activeTab !== "notifications" && activeTab !== "grants" && (
            <Button size="xs" variant="light" color="gray" loading={loading} onClick={refreshActiveTab}>
              Refresh
            </Button>
          )}
        </div>
        <Tabs.List className="haku-settings-tabs-list" aria-label="Settings sections">
          <Tabs.Tab value="mcp">
            <span className="haku-settings-tab-long">MCP servers</span>
            <span className="haku-settings-tab-short">MCP</span>
          </Tabs.Tab>
          <Tabs.Tab value="agents">Agents</Tabs.Tab>
          <Tabs.Tab value="grants">
            <span className="haku-settings-tab-long">Grants</span>
            <span className="haku-settings-tab-short">Grants</span>
          </Tabs.Tab>
          <Tabs.Tab value="notifications">
            <span className="haku-settings-tab-long">Notifications</span>
            <span className="haku-settings-tab-short">Alerts</span>
          </Tabs.Tab>
          <Tabs.Tab value="nodes">Nodes</Tabs.Tab>
          <Tabs.Tab value="system">System</Tabs.Tab>
        </Tabs.List>
      </header>
      <div className="haku-page-scroll">
        <Tabs.Panel value="mcp">
          <ResourcePanel
            title="MCP servers"
            description="Live availability and account linkage for the tools Haku exposes. Status refreshes automatically and may verify linked credentials."
            resource={mcpResource}
            label="MCP servers"
            emptyMessage="No MCP servers are configured."
          >
            {(views) =>
              views.map((view) => (
                <McpServerCard
                  key={view.connection.server_id}
                  view={view}
                  onConnectMcp={connect}
                  onDisconnectMcp={disconnect}
                  onConnectProvider={connectProvider}
                  onDisconnectProvider={disconnectProvider}
                />
              ))
            }
          </ResourcePanel>
        </Tabs.Panel>
        <Tabs.Panel value="agents">
          <ResourcePanel
            title="Agents"
            description="Clients authorized to use Haku. Activity is historical and does not indicate that a client is currently online."
            resource={agentsResource}
            label="Agents"
            emptyMessage="No Agents have been authorized."
            isEmpty={(response) => response.agents.length === 0}
          >
            {(items) =>
              items.agents.map((agent) => (
                <AgentCard
                  key={agent.agent_id}
                  agent={agent}
                  accessProfiles={agentAccessProfiles}
                  saving={savingAgentId !== null}
                  onAccessProfileChange={changeAgentAccessProfile}
                />
              ))
            }
          </ResourcePanel>
        </Tabs.Panel>
        <Tabs.Panel value="grants">
          <GrantsPanel />
        </Tabs.Panel>
        <Tabs.Panel value="notifications">
          <Stack gap="xs" className="haku-page-list">
            <SectionHeading
              title="Notifications"
              description="Get notified on this device when a tool call needs approval, even when the console is closed. Enable notifications separately on every browser you want reached."
            />
            <PushNotificationCard />
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="nodes">
          <ResourcePanel
            title="Node daemons"
            description="Outbound execution workers. Heartbeats determine whether approved node work can currently be dispatched."
            resource={daemonsResource}
            label="node daemons"
            emptyMessage="No node daemons are configured."
          >
            {(items) => items.map((daemon) => <DaemonCard key={daemon.daemon_id} daemon={daemon} />)}
          </ResourcePanel>
        </Tabs.Panel>
        <Tabs.Panel value="system">
          <Stack gap="xs" className="haku-page-list">
            <SectionHeading
              title="System"
              description="Deployment status for the Console server and web application, plus semantic recall indexes."
            />
            <ResourcePanel resource={deploymentResource} label="system status">
              {(value) => <SystemStatusCard deployment={value} />}
            </ResourcePanel>
            <SectionHeading title="Indexes" description="How current each configured semantic recall corpus is." />
            <ResourcePanel
              resource={indexStatusResource}
              label="index status"
              emptyMessage="No semantic recall indexes are configured."
              isEmpty={(value) => value.indexes.length === 0}
            >
              {(value) => value.indexes.map((index) => <IndexStatusCard key={index.index_id} index={index} />)}
            </ResourcePanel>
          </Stack>
        </Tabs.Panel>
      </div>
    </Tabs>
  );
}
