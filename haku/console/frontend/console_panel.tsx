import { Badge, Button, Divider, Drawer, Group, Stack, Text } from "@mantine/core";

import type { McpOperatorAuthStatus } from "./client.ts";

// The shell's own operator control surface — trusted chrome the agent-owned iframe cannot
// render into, opened by the floating escape button over the (full-page) frame. It hosts
// shell-owned status and controls that must live outside Haku's reach. New shell config/
// views go here as sections, rather than scattering more floating badges over the frame.
//
// This is NOT a consent surface (it only *reveals* state and *reduces* privilege), so it
// needn't be a top-layer `<dialog>` like the ConfirmDialog — a portalled Drawer above the
// iframe is enough. The one authority moment (granting location) stays in ConfirmDialog.
export interface ConsolePanelProps {
  opened: boolean;
  onClose: () => void;
  // Standing location-sharing grant (geolocation_grant.ts), whether a live watch is currently
  // streaming, and the withdraw action (revokes the grant AND stops any live watch).
  geoGranted: boolean;
  tracking: boolean;
  onWithdrawGeolocation: () => void;
  mcpAuthStatuses: McpOperatorAuthStatus[];
  onConnectMcp: (serverId: string) => void;
  onDisconnectMcp: (serverId: string) => void;
  onRefreshMcp: () => void;
}

// zIndex maxed so the Drawer sits above the full-page iframe; the escape button is one below.
export const PANEL_Z = 2147483647;

function shortDate(value: string | null | undefined): string | null {
  if (!value) return null;
  return new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export function ConsolePanel({
  opened,
  onClose,
  geoGranted,
  tracking,
  onWithdrawGeolocation,
  mcpAuthStatuses,
  onConnectMcp,
  onDisconnectMcp,
  onRefreshMcp,
}: ConsolePanelProps) {
  return (
    <Drawer opened={opened} onClose={onClose} position="right" size="sm" title="Console" zIndex={PANEL_Z}>
      <Stack gap="lg">
        <Stack gap={6}>
          <Text fw={600} size="sm">
            Location sharing
          </Text>
          {geoGranted ? (
            <Group justify="space-between">
              {tracking ? (
                <Badge color="teal" variant="filled" leftSection="📍">
                  Tracking — live
                </Badge>
              ) : (
                <Badge color="blue" variant="light" leftSection="📍">
                  Allowed — idle
                </Badge>
              )}
              <Button size="xs" variant="light" color="red" onClick={onWithdrawGeolocation}>
                {tracking ? "Stop & withdraw" : "Withdraw"}
              </Button>
            </Group>
          ) : (
            <Text size="sm" c="dimmed">
              Not shared — Haku will ask when it needs your location.
            </Text>
          )}
        </Stack>
        <Divider />
        <Stack gap="sm">
          <Group justify="space-between">
            <Text fw={600} size="sm">
              MCP accounts
            </Text>
            <Button size="compact-xs" variant="subtle" onClick={onRefreshMcp}>
              Refresh
            </Button>
          </Group>
          {mcpAuthStatuses.length === 0 ? (
            <Text size="sm" c="dimmed">
              No operator-linked MCP servers are configured.
            </Text>
          ) : (
            mcpAuthStatuses.map((status) => (
              <Stack key={status.server_id} gap={5}>
                <Group justify="space-between" align="flex-start" gap="xs">
                  <Stack gap={2}>
                    <Text size="sm" fw={500}>
                      {status.server_id}
                    </Text>
                    <Text size="xs" c="dimmed">
                      {status.status === "connected"
                        ? `Linked for ${status.operator_principal}${
                            shortDate(status.token_expires_at) ? ` until ${shortDate(status.token_expires_at)}` : ""
                          }`
                        : `Not linked for ${status.operator_principal}`}
                    </Text>
                  </Stack>
                  {status.status === "connected" ? (
                    <Badge color="teal" variant="light">
                      Connected
                    </Badge>
                  ) : (
                    <Badge color="gray" variant="light">
                      Unconnected
                    </Badge>
                  )}
                </Group>
                <Group justify="flex-end" gap="xs">
                  {status.status === "connected" ? (
                    <>
                      <Button size="xs" variant="light" onClick={() => onConnectMcp(status.server_id)}>
                        Reconnect
                      </Button>
                      <Button size="xs" variant="subtle" color="red" onClick={() => onDisconnectMcp(status.server_id)}>
                        Disconnect
                      </Button>
                    </>
                  ) : (
                    <Button size="xs" variant="light" onClick={() => onConnectMcp(status.server_id)}>
                      Connect
                    </Button>
                  )}
                </Group>
              </Stack>
            ))
          )}
        </Stack>
      </Stack>
    </Drawer>
  );
}
