import { ActionIcon, Badge, Button, Group, Loader, Select, Stack, Table, Tabs, Text } from "@mantine/core";
import { useCallback, useEffect, useState, type ReactNode } from "react";

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
import { DisconnectIcon } from "./icons";
import { ExternalLink } from "./link";
import { usePushNotifications, type PushState } from "./push_subscription";
import { formatTimestamp, shortDate } from "./time";
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
import { listActiveSandboxes, terminateSandbox, type ActiveSandbox } from "./session_sandboxes_client";
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

function SectionHeading({ title, description }: { title: string; description?: string }) {
  return (
    <div>
      <Text fw={600}>{title}</Text>
      {description && (
        <Text size="xs" c="dimmed" mt={4}>
          {description}
        </Text>
      )}
    </div>
  );
}

function DenseTable({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <Table.ScrollContainer minWidth={0} className="haku-dense-table-wrap">
      <Table className="haku-dense-table" aria-label={label} highlightOnHover>
        {children}
      </Table>
    </Table.ScrollContainer>
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
      <div className="haku-empty-state">
        <Text size="sm" c="dimmed">
          {emptyMessage}
        </Text>
      </div>
    ) : (
      children(data)
    );
  return title ? (
    <Stack gap="xs" className="haku-page-list">
      <SectionHeading title={title} description={description} />
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
  const connection = server.connection;
  if (connection === null) {
    return server.backend.kind === "remote_mcp" && server.backend.auth.kind === "static_bearer"
      ? "Console-managed credential"
      : "No operator-linked account";
  }
  if (isMcpOperatorAuthStatus(connection)) {
    const linkedUntil = shortDate(
      typeof connection.state.token_expires_at === "string" ? connection.state.token_expires_at : null
    );
    switch (connection.state.status) {
      case "unconnected":
        return `Not linked · ${connection.username}`;
      case "degraded":
        return `Linked · ${connection.username} · refresh failing${linkedUntil ? ` · token until ${linkedUntil}` : ""}`;
      case "connected":
        return `Linked · ${connection.username}${linkedUntil ? ` until ${linkedUntil}` : ""}`;
    }
  }
  const until = shortDate(typeof connection.token_expires_at === "string" ? connection.token_expires_at : null);
  switch (connection.status) {
    case "unprovisioned":
      return `${connection.display_name} OAuth client is not provisioned`;
    case "unconnected":
      return `${connection.display_name} is not connected`;
    case "degraded":
      return `${connection.display_name} refresh failing${until ? ` · token until ${until}` : ""}`;
    case "connected":
      return `${connection.display_name} connected${until ? ` · token until ${until}` : ""}`;
  }
}

function McpServerRow({
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
  const statusMarker = view.checking ? (
    <Loader size={12} aria-label="Checking connection status" />
  ) : (
    <span
      className="haku-status-dot"
      data-color={state.color}
      role="img"
      aria-label={state.label}
      title={state.label}
    />
  );

  return (
    <Table.Tr>
      <Table.Td data-slot="primary" className="haku-dense-primary">
        <Group gap="xs" wrap="nowrap" className="haku-mcp-server-name">
          {statusMarker}
          <div className="haku-mcp-server-name-text">
            <Text fw={600} size="sm">
              {view.connection.server_id}
            </Text>
            <Text size="xs" c="dimmed">
              {view.connection.backend.kind === "remote_mcp" ? "Remote MCP" : "In-process"}
            </Text>
          </div>
        </Group>
      </Table.Td>
      <Table.Td data-slot="secondary" className="haku-dense-secondary">
        <Text size="sm">{connectionSummary(view.connection)}</Text>
        {reason && (
          <Text size="xs" c="red">
            {reason}
          </Text>
        )}
      </Table.Td>
      <Table.Td data-slot="action" className="haku-dense-action">
        {linkage &&
          !unprovisioned &&
          (linkageStatus === "connected" || linkageStatus === "degraded" ? (
            <ActionIcon
              size="sm"
              variant="subtle"
              color="red"
              aria-label="Disconnect MCP account"
              title="Disconnect MCP account"
              onClick={disconnect ?? undefined}
            >
              <DisconnectIcon size={16} />
            </ActionIcon>
          ) : (
            <Button size="compact-sm" variant="light" onClick={connect ?? undefined}>
              Connect
            </Button>
          ))}
      </Table.Td>
    </Table.Tr>
  );
}

const DAEMON_STATUS_COLOR: Record<DaemonStatus["status"], string> = {
  connected: "teal",
  busy: "blue",
  stale: "yellow",
  offline: "gray",
};

function DaemonRow({ daemon }: { daemon: DaemonStatus }) {
  const seen = daemon.last_heartbeat_at ? formatTimestamp(daemon.last_heartbeat_at) : null;
  return (
    <Table.Tr className="haku-node-row">
      <Table.Td data-slot="primary" className="haku-dense-primary">
        <Text fw={600} size="sm">
          {daemon.display_name}
        </Text>
      </Table.Td>
      <Table.Td data-slot="status" className="haku-dense-status">
        <span
          className="haku-status-dot haku-node-status-dot"
          data-color={DAEMON_STATUS_COLOR[daemon.status]}
          data-status={daemon.status}
          role="img"
          aria-label={`Node status: ${daemon.status}`}
          title={`Node status: ${daemon.status}`}
        />
      </Table.Td>
      <Table.Td data-slot="version" className="haku-dense-secondary haku-dense-version">
        <Text size="sm">{daemon.version ?? "—"}</Text>
      </Table.Td>
      <Table.Td data-slot="heartbeat" className="haku-dense-secondary haku-dense-heartbeat">
        <Text size="sm" title={seen?.title}>
          {seen?.text ?? "—"}
        </Text>
      </Table.Td>
      <Table.Td data-slot="action" className="haku-dense-action">
        {daemon.active_execution_id ? (
          <Text size="xs" c="dimmed" ff="monospace" title={daemon.active_execution_id}>
            {daemon.active_execution_id.slice(0, 12)}…
          </Text>
        ) : (
          <Text size="xs" c="dimmed">
            —
          </Text>
        )}
      </Table.Td>
    </Table.Tr>
  );
}

const AGENT_STATUS_COLOR: Record<AgentView["status"], string> = {
  draft: "blue",
  active: "teal",
  abandoned: "gray",
  disabled: "orange",
  deleted: "gray",
};

function AgentRow({
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
    <Table.Tr>
      <Table.Td data-slot="primary" className="haku-dense-primary">
        <Text fw={600} size="sm">
          {agent.display_name}
        </Text>
        <Text size="xs" c="dimmed">
          {agent.credential_kind === "oauth" ? "OAuth" : "Static credential"}
          {activated ? ` · active since ${activated}` : ""}
        </Text>
      </Table.Td>
      <Table.Td data-slot="status" className="haku-dense-status">
        <Badge color={AGENT_STATUS_COLOR[agent.status]} variant="light">
          {agent.status}
        </Badge>
      </Table.Td>
      <Table.Td data-slot="secondary" className="haku-dense-secondary">
        {lastSeen ? `last seen ${lastSeen}` : "Not seen yet"}
      </Table.Td>
      <Table.Td data-slot="action" className="haku-dense-action">
        {agent.credential_kind === "static" ? (
          <Text size="xs" c="dimmed" title="Managed by deployment configuration.">
            deployment
          </Text>
        ) : (
          <Select
            size="xs"
            aria-label={`Access profile for ${agent.display_name}`}
            data={accessProfiles.map((profile) => ({
              value: profile,
              label: profile.replaceAll("_", " "),
            }))}
            value={agent.access_profile_id}
            placeholder="Profile"
            onChange={(profile) => {
              if (profile && profile !== agent.access_profile_id) onAccessProfileChange(agent, profile);
            }}
            allowDeselect={false}
            disabled={saving}
          />
        )}
      </Table.Td>
    </Table.Tr>
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

function PushNotificationTable() {
  const { state, devices, enable, disable, forget } = usePushNotifications();
  const display = PUSH_STATE_DISPLAY[state.status];
  const actionable = state.status === "on" || state.status === "off" || state.status === "failed";
  const thisEndpoint = state.status === "on" ? state.endpoint : null;
  const others = devices.filter((device) => device.endpoint !== thisEndpoint);
  return (
    <DenseTable label="Notification devices">
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Device</Table.Th>
          <Table.Th>Status</Table.Th>
          <Table.Th>Added</Table.Th>
          <Table.Th />
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        <Table.Tr>
          <Table.Td data-slot="primary" className="haku-dense-primary">
            <Text fw={600} size="sm">
              This browser
            </Text>
            {state.status === "failed" && (
              <Text size="xs" c="red" title={display.description}>
                {state.message}
              </Text>
            )}
          </Table.Td>
          <Table.Td data-slot="status" className="haku-dense-status">
            <Badge color={display.color} variant="light" title={display.description}>
              {display.label}
            </Badge>
          </Table.Td>
          <Table.Td data-slot="secondary" className="haku-dense-secondary">
            —
          </Table.Td>
          <Table.Td data-slot="action" className="haku-dense-action">
            {actionable &&
              (state.status === "on" ? (
                <Button size="compact-sm" variant="subtle" color="red" onClick={() => void disable()}>
                  Turn off
                </Button>
              ) : (
                <Button size="compact-sm" variant="light" onClick={() => void enable()}>
                  Turn on
                </Button>
              ))}
          </Table.Td>
        </Table.Tr>
        {others.map((device) => (
          <Table.Tr key={device.endpoint}>
            <Table.Td data-slot="primary" className="haku-dense-primary">
              <Text size="sm">{device.userAgent ?? "Unidentified browser"}</Text>
            </Table.Td>
            <Table.Td data-slot="status" className="haku-dense-status">
              <Badge color="teal" variant="light">
                On
              </Badge>
            </Table.Td>
            <Table.Td data-slot="secondary" className="haku-dense-secondary">
              {shortDate(device.createdAt) ? `added ${shortDate(device.createdAt)}` : "—"}
            </Table.Td>
            <Table.Td data-slot="action" className="haku-dense-action">
              <Button size="compact-sm" variant="subtle" color="red" onClick={() => void forget(device.endpoint)}>
                Forget
              </Button>
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </DenseTable>
  );
}

type SessionStatusDisplay = { label: string; color: string; description: string };

export function activeSandboxStatusDisplay(status: ActiveSandbox["status"]): SessionStatusDisplay {
  switch (status) {
    case "provisioning":
      return { label: "Provisioning", color: "blue", description: "The sandbox claim is being handed to a runner." };
    case "ready":
      return { label: "Ready", color: "teal", description: "The sandbox is ready for the next turn." };
    case "responding":
      return { label: "Responding", color: "blue", description: "The runner is handling an open turn." };
    case "closing":
      return { label: "Closing", color: "orange", description: "Termination is deleting the sandbox claim." };
    case "idle":
      return { label: "Idle", color: "gray", description: "The session has not allocated a sandbox yet." };
    case "closed":
      return { label: "Closed", color: "gray", description: "The session is closed." };
    case "failed":
      return { label: "Failed", color: "red", description: "The session ended with an error." };
  }
}

export function provisioningStepLabel(step: ActiveSandbox["sandbox"]["step"]): string {
  switch (step) {
    case "claim_created":
      return "Claim created";
    case "waiting_for_sandbox":
      return "Waiting for Sandbox";
    case "waiting_for_pod":
      return "Waiting for Pod";
    case "waiting_for_pod_ready":
      return "Waiting for Pod readiness";
    case "waiting_for_runner":
      return "Waiting for runner";
    case "claim_absent":
      return "Claim absent";
  }
}

function SandboxSessionRow({
  session,
  terminationPending,
  onRequestTerminate,
  onCancelTerminate,
  onConfirmTerminate,
}: {
  session: ActiveSandbox;
  terminationPending: boolean;
  onRequestTerminate: () => void;
  onCancelTerminate: () => void;
  onConfirmTerminate: () => void;
}) {
  const display = activeSandboxStatusDisplay(session.status);
  const closing = session.status === "closing";
  return (
    <Table.Tr className={terminationPending ? "haku-session-row-termination-pending" : undefined}>
      <Table.Td data-slot="primary" className="haku-dense-primary">
        <Text fw={600} size="sm">
          {session.harness_kind.replaceAll("_", " ")}
        </Text>
        <Text size="xs" c="dimmed" ff="monospace" className="break-all">
          {session.session_id}
        </Text>
      </Table.Td>
      <Table.Td data-slot="status" className="haku-dense-status">
        <Badge color={display.color} variant="light" title={display.description}>
          {display.label}
        </Badge>
      </Table.Td>
      <Table.Td data-slot="secondary" className="haku-dense-secondary">
        <Text size="sm">{provisioningStepLabel(session.sandbox.step)}</Text>
        <Text size="xs">started {shortDate(session.created_at) ?? "unknown"}</Text>
      </Table.Td>
      <Table.Td data-slot="action" className="haku-dense-action">
        {closing ? (
          <Button size="compact-sm" color="red" variant="light" disabled>
            Closing…
          </Button>
        ) : terminationPending ? (
          <Group gap="xs" wrap="nowrap">
            <Button size="compact-sm" color="red" onClick={onConfirmTerminate}>
              Yes, terminate
            </Button>
            <Button size="compact-sm" color="gray" variant="subtle" onClick={onCancelTerminate}>
              Cancel
            </Button>
          </Group>
        ) : (
          <Button size="compact-sm" color="red" variant="light" onClick={onRequestTerminate}>
            Terminate
          </Button>
        )}
      </Table.Td>
    </Table.Tr>
  );
}

function SessionsPanel({ resource }: { resource: AsyncResource<ActiveSandbox[]> }) {
  const [pendingTermination, setPendingTermination] = useState<ActiveSandbox | null>(null);

  function requestTermination(session: ActiveSandbox) {
    if (session.status === "closing") return;
    setPendingTermination(session);
  }

  function approveTermination() {
    const session = pendingTermination;
    setPendingTermination(null);
    if (!session) return;
    resource.update(
      (current) =>
        current?.map((item) =>
          item.session_id === session.session_id ? { ...item, status: "closing" as const } : item
        ) ?? null
    );
    void terminateSandbox(session.session_id).then(
      () => {
        toastSuccess("Sandbox termination started", "The active session will disappear when its claim is gone.");
        resource.refresh();
      },
      (error: unknown) => {
        toastError("Couldn't terminate sandbox", error);
        resource.refresh();
      }
    );
  }

  return (
    <Stack gap="xs" className="haku-page-list">
      <SectionHeading title="Sessions" />
      <ResourcePanel
        resource={resource}
        label="sessions"
        emptyMessage="No active sandbox sessions."
        isEmpty={(sessions) => sessions.length === 0}
      >
        {(sessions) => (
          <DenseTable label="Active sessions">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Runtime</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th>Progress</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {sessions.map((session) => (
                <SandboxSessionRow
                  key={session.session_id}
                  session={session}
                  terminationPending={pendingTermination?.session_id === session.session_id}
                  onRequestTerminate={() => requestTermination(session)}
                  onCancelTerminate={() => setPendingTermination(null)}
                  onConfirmTerminate={approveTermination}
                />
              ))}
            </Table.Tbody>
          </DenseTable>
        )}
      </ResourcePanel>
    </Stack>
  );
}

const SETTINGS_TABS = ["mcp", "agents", "grants", "sessions", "notifications", "nodes", "system"] as const;
type SettingsTab = (typeof SETTINGS_TABS)[number];

export function settingsTabFromSearch(search: string): SettingsTab {
  const requested = new URLSearchParams(search).get("tab");
  return SETTINGS_TABS.find((tab) => tab === requested) ?? "mcp";
}

function settingsTabFromLocation(): SettingsTab {
  return settingsTabFromSearch(window.location.search);
}

function SystemStatusTable({ deployment }: { deployment: DeploymentInfo }) {
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
    <DenseTable label="Deployment status">
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Component</Table.Th>
          <Table.Th>Revision</Table.Th>
          <Table.Th>Status</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {versions.map((version) => (
          <Table.Tr key={version.label}>
            <Table.Td data-slot="primary" className="haku-dense-primary">
              <Text size="sm">{version.label}</Text>
            </Table.Td>
            <Table.Td data-slot="secondary" className="haku-dense-secondary">
              <VersionLink version={version} />
            </Table.Td>
            <Table.Td data-slot="status" className="haku-dense-status">
              <Badge color={status.color} variant="light" title={status.description}>
                {status.label}
              </Badge>
            </Table.Td>
          </Table.Tr>
        ))}
        {versions.length === 0 && (
          <Table.Tr>
            <Table.Td colSpan={3}>
              <Text size="sm" c="dimmed">
                Deployment metadata unavailable.
              </Text>
            </Table.Td>
          </Table.Tr>
        )}
      </Table.Tbody>
    </DenseTable>
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

function IndexStatusRow({ index }: { index: IndexState }) {
  const status = indexStatusDisplay(index);
  const indexedAt = shortDate((index.index_type === "git" ? index.indexed_at : index.last_indexed_at) ?? null);
  return (
    <Table.Tr>
      <Table.Td data-slot="primary" className="haku-dense-primary">
        <Text fw={600} size="sm">
          {index.index_id}
        </Text>
        <Text size="xs" c="dimmed">
          {index.index_type}
        </Text>
      </Table.Td>
      <Table.Td data-slot="status" className="haku-dense-status">
        <Badge color={status.color} variant="light" title={status.description}>
          {status.label}
        </Badge>
      </Table.Td>
      <Table.Td data-slot="secondary" className="haku-dense-secondary">
        {index.index_type === "git" ? (
          <>
            <Text size="sm">
              {index.branch ?? "Git"} · {index.files ?? 0} files · {index.chunks ?? 0} chunks ·{" "}
              {index.embedded_chunks ?? 0} embedded
              {(index.pending_chunks ?? 0) > 0 ? ` · ${index.pending_chunks} pending` : ""}
            </Text>
            <Text size="xs" ff="monospace">
              indexed {commitLabel(index.indexed_commit)}
              {index.remote_commit !== index.indexed_commit ? ` · remote ${commitLabel(index.remote_commit)}` : ""}
            </Text>
          </>
        ) : (
          <>
            <Text size="sm">
              {index.sessions} sessions · {index.chunks} chunks · {index.embedded_chunks} embedded
              {index.pending_chunks > 0 ? ` · ${index.pending_chunks} pending` : ""}
            </Text>
            {(index.stale_sessions > 0 || index.unindexed_messages > 0) && (
              <Text size="xs">
                {index.stale_sessions} stale sessions · {index.unindexed_messages} messages pending
              </Text>
            )}
          </>
        )}
        <Text size="xs">
          {indexedAt ? `last indexed ${indexedAt}` : "Not indexed yet"}
          {(index.superseded_chunks ?? 0) > 0 ? ` · ${index.superseded_chunks} superseded chunks` : ""}
        </Text>
      </Table.Td>
    </Table.Tr>
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
  const sessionsResource = useAsyncResource(listActiveSandboxes, resourceOptions("sessions", 10_000));
  const refreshMcp = mcpResource.refresh;
  const refreshAgents = agentsResource.refresh;
  const refreshDeployment = deploymentResource.refresh;
  const refreshIndexStatus = indexStatusResource.refresh;
  const refreshDaemons = daemonsResource.refresh;
  const refreshSessions = sessionsResource.refresh;
  const agentAccessProfiles = agentsResource.data?.access_profiles ?? [];
  const refreshActiveTab = useCallback(() => {
    if (activeTab === "mcp") return refreshMcp();
    if (activeTab === "agents") return refreshAgents();
    if (activeTab === "nodes") return refreshDaemons();
    if (activeTab === "sessions") return refreshSessions();
    if (activeTab === "system") {
      refreshDeployment();
      refreshIndexStatus();
    }
  }, [activeTab, refreshAgents, refreshDaemons, refreshDeployment, refreshIndexStatus, refreshMcp, refreshSessions]);
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
    if (activeTab === "sessions" && event.event_type === "sandbox_sessions_changed") refreshSessions();
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
        : activeTab === "sessions"
          ? sessionsResource.loading
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
          <Tabs.Tab value="sessions">Sessions</Tabs.Tab>
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
            resource={mcpResource}
            label="MCP servers"
            emptyMessage="No MCP servers are configured."
          >
            {(views) => (
              <DenseTable label="MCP servers">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Server</Table.Th>
                    <Table.Th>Connection</Table.Th>
                    <Table.Th />
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {views.map((view) => (
                    <McpServerRow
                      key={view.connection.server_id}
                      view={view}
                      onConnectMcp={connect}
                      onDisconnectMcp={disconnect}
                      onConnectProvider={connectProvider}
                      onDisconnectProvider={disconnectProvider}
                    />
                  ))}
                </Table.Tbody>
              </DenseTable>
            )}
          </ResourcePanel>
        </Tabs.Panel>
        <Tabs.Panel value="agents">
          <ResourcePanel
            title="Agents"
            resource={agentsResource}
            label="Agents"
            emptyMessage="No Agents have been authorized."
            isEmpty={(response) => response.agents.length === 0}
          >
            {(items) => (
              <DenseTable label="Agents">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Agent</Table.Th>
                    <Table.Th>Status</Table.Th>
                    <Table.Th>Activity</Table.Th>
                    <Table.Th>Access profile</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {items.agents.map((agent) => (
                    <AgentRow
                      key={agent.agent_id}
                      agent={agent}
                      accessProfiles={agentAccessProfiles}
                      saving={savingAgentId !== null}
                      onAccessProfileChange={changeAgentAccessProfile}
                    />
                  ))}
                </Table.Tbody>
              </DenseTable>
            )}
          </ResourcePanel>
        </Tabs.Panel>
        <Tabs.Panel value="grants">
          <GrantsPanel />
        </Tabs.Panel>
        <Tabs.Panel value="sessions">
          <SessionsPanel resource={sessionsResource} />
        </Tabs.Panel>
        <Tabs.Panel value="notifications">
          <Stack gap="xs" className="haku-page-list">
            <SectionHeading title="Notifications" />
            <PushNotificationTable />
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="nodes">
          <ResourcePanel
            title="Node daemons"
            resource={daemonsResource}
            label="node daemons"
            emptyMessage="No node daemons are configured."
          >
            {(items) => (
              <DenseTable label="Node daemons">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Node</Table.Th>
                    <Table.Th>Status</Table.Th>
                    <Table.Th>Version</Table.Th>
                    <Table.Th>Heartbeat</Table.Th>
                    <Table.Th>Active work</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {items.map((daemon) => (
                    <DaemonRow key={daemon.daemon_id} daemon={daemon} />
                  ))}
                </Table.Tbody>
              </DenseTable>
            )}
          </ResourcePanel>
        </Tabs.Panel>
        <Tabs.Panel value="system">
          <Stack gap="xs" className="haku-page-list">
            <SectionHeading title="System" />
            <ResourcePanel resource={deploymentResource} label="system status">
              {(value) => <SystemStatusTable deployment={value} />}
            </ResourcePanel>
            <SectionHeading title="Indexes" />
            <ResourcePanel
              resource={indexStatusResource}
              label="index status"
              emptyMessage="No semantic recall indexes are configured."
              isEmpty={(value) => value.indexes.length === 0}
            >
              {(value) => (
                <DenseTable label="Semantic recall indexes">
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Index</Table.Th>
                      <Table.Th>Status</Table.Th>
                      <Table.Th>Contents / freshness</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {value.indexes.map((index) => (
                      <IndexStatusRow key={index.index_id} index={index} />
                    ))}
                  </Table.Tbody>
                </DenseTable>
              )}
            </ResourcePanel>
          </Stack>
        </Tabs.Panel>
      </div>
    </Tabs>
  );
}
