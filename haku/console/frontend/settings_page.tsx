import { Badge, Button, Group, Loader, Stack, Text } from "@mantine/core";
import { useCallback, useEffect, useRef, useState } from "react";

import { shortDate } from "./approval_state.ts";
import {
  disconnectMcpOperatorAuth,
  fetchMcpOperatorAuthStatuses,
  type McpOperatorAuthStatus,
  startMcpOperatorAuth,
} from "./client.ts";
import { ArrowLeftIcon } from "./icons.tsx";
import { openExternal, POPUP_HINT } from "./open_external.ts";
import { toastError, toastSuccess } from "./toast.ts";

// Other console tabs (and the OAuth-callback page) post here when an MCP account link
// changes, so an open Settings page refetches without a manual reload.
const MCP_AUTH_CHANNEL = "haku-console-mcp-auth";

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
              ? `Linked for ${status.operator_principal}${until ? ` until ${until}` : ""}`
              : `Not linked for ${status.operator_principal}`}
          </Text>
        </Stack>
        <Badge color={connected ? "teal" : "gray"} variant="light">
          {connected ? "Connected" : "Unconnected"}
        </Badge>
      </Group>
      <Group justify="flex-end" gap="xs" mt="sm">
        {connected ? (
          <>
            <Button size="compact-sm" variant="light" onClick={onConnect}>
              Reconnect
            </Button>
            <Button size="compact-sm" variant="subtle" color="red" onClick={onDisconnect}>
              Disconnect
            </Button>
          </>
        ) : (
          <Button size="compact-sm" variant="light" onClick={onConnect}>
            Connect
          </Button>
        )}
      </Group>
    </section>
  );
}

// Operator settings — the console's rarely-touched MCP account linkage, moved out of the
// approval drawer into its own full-page route (routing.ts → "/settings").
export function SettingsPage({ onBack }: { onBack: () => void }) {
  const [statuses, setStatuses] = useState<McpOperatorAuthStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const channelRef = useRef<BroadcastChannel | null>(null);

  const load = useCallback((notifyPeers = false) => {
    if (notifyPeers) channelRef.current?.postMessage({ type: "mcpAuthChanged" });
    setLoading(true);
    fetchMcpOperatorAuthStatuses().then(
      (s) => {
        setStatuses(s);
        setError(null);
        setLoading(false);
      },
      (e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      }
    );
  }, []);

  useEffect(() => {
    load();
    if (!("BroadcastChannel" in window)) return;
    const channel = new BroadcastChannel(MCP_AUTH_CHANNEL);
    channelRef.current = channel;
    channel.onmessage = () => load();
    return () => {
      if (channelRef.current === channel) channelRef.current = null;
      channel.close();
    };
  }, [load]);

  function connect(serverId: string) {
    startMcpOperatorAuth(serverId).then(
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
        load(true);
      },
      (e: unknown) => toastError("Couldn't disconnect MCP account", e)
    );
  }

  return (
    <div className="haku-page">
      <header className="haku-page-header">
        <div className="haku-page-bar">
          <Group gap="xs" wrap="nowrap" align="center">
            <Button size="xs" variant="subtle" color="gray" leftSection={<ArrowLeftIcon />} onClick={onBack}>
              Back
            </Button>
            <Text fw={700}>Settings</Text>
          </Group>
          <Button size="xs" variant="light" loading={loading} onClick={() => load()}>
            Refresh
          </Button>
        </div>
      </header>
      <div className="haku-page-scroll">
        <div className="haku-page-list">
          <section className="haku-shell-card">
            <Text fw={600}>MCP accounts</Text>
            <Text size="xs" c="dimmed" mt={4}>
              Operator OAuth links for connected MCP servers. Connect one to let the console execute its tools with your
              account.
            </Text>
          </section>
          {error && (
            <Text c="red" size="sm">
              Failed to load MCP accounts: {error}
            </Text>
          )}
          {!statuses && !error && (
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
        </div>
      </div>
    </div>
  );
}
