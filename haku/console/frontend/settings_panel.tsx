import { Anchor, Badge, Button, Group, Loader, Stack, Text } from "@mantine/core";
import { useCallback, useEffect, useRef, useState } from "react";

import { shortDate } from "./approval_state.ts";
import {
  disconnectMcpOperatorAuth,
  fetchDeploymentInfo,
  fetchMcpOperatorAuthStatuses,
  type DeploymentInfo,
  type McpOperatorAuthStatus,
  connectMcpOperatorAuth,
} from "./client.ts";
import { useConsoleEvents } from "./console_events.ts";
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
    <Anchor href={version.image.source_commit_url} target="_blank" rel="noreferrer" size="xs" ff="monospace">
      {commit}
    </Anchor>
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
    <section className="haku-shell-card">
      <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
        <Stack gap={2} style={{ minWidth: 0 }}>
          <Text fw={600}>{status.server_id}</Text>
          <Text size="xs" c="dimmed">
            {connected
              ? `Linked for ${status.username}${until ? ` until ${until}` : ""}`
              : `Not linked for ${status.username}`}
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

// Operator settings — the console's rarely-touched MCP account linkage, rendered as one of the
// shell chrome's mutually exclusive panels.
export function SettingsPanel() {
  const [statuses, setStatuses] = useState<McpOperatorAuthStatus[] | null>(null);
  const [deployment, setDeployment] = useState<DeploymentInfo | null>(null);
  const [statusesError, setStatusesError] = useState<string | null>(null);
  const [deploymentError, setDeploymentError] = useState<string | null>(null);
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
    void Promise.all([statusesRequest, deploymentRequest]).then(() => {
      if (generation === loadGeneration.current) setLoading(false);
    });
  }, []);

  useEffect(
    () => () => {
      loadGeneration.current += 1;
    },
    []
  );

  useConsoleEvents((event) => {
    if (event.event_type === "sync" || event.event_type === "mcp_operator_auth_changed") load();
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
          <section className="haku-shell-card">
            <Text fw={600}>MCP accounts</Text>
            <Text size="xs" c="dimmed" mt={4}>
              Operator OAuth links for connected MCP servers. Connect one to let the console execute its tools with your
              account.
            </Text>
          </section>
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
          {deployment && (
            <section className="haku-shell-card">
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
            </section>
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
