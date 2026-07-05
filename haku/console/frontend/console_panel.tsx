import { Badge, Button, Drawer, Group, Stack, Text } from "@mantine/core";

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
  // Standing location-sharing grant (geolocation_grant.ts) + its withdraw action.
  geoGranted: boolean;
  onWithdrawGeolocation: () => void;
}

// zIndex maxed so the Drawer sits above the full-page iframe; the escape button is one below.
export const PANEL_Z = 2147483647;

export function ConsolePanel({ opened, onClose, geoGranted, onWithdrawGeolocation }: ConsolePanelProps) {
  return (
    <Drawer opened={opened} onClose={onClose} position="right" size="sm" title="Console" zIndex={PANEL_Z}>
      <Stack gap="lg">
        <Stack gap={6}>
          <Text fw={600} size="sm">
            Location sharing
          </Text>
          {geoGranted ? (
            <Group justify="space-between">
              <Badge color="blue" variant="light" leftSection="📍">
                Allowed
              </Badge>
              <Button size="xs" variant="light" color="red" onClick={onWithdrawGeolocation}>
                Withdraw
              </Button>
            </Group>
          ) : (
            <Text size="sm" c="dimmed">
              Not shared — Haku will ask when it needs your location.
            </Text>
          )}
        </Stack>
      </Stack>
    </Drawer>
  );
}
