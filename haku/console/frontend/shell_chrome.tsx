import { ActionIcon, Badge, Button, Group, Indicator, Stack, Text, Tooltip } from "@mantine/core";
import { type JSX, type ReactNode, useEffect, useMemo, useState } from "react";

import {
  capabilityRequestItems,
  geolocationApprovalBody,
  geolocationApprovalTitle,
  SCREENSHOT_APPROVAL_BODY,
  SCREENSHOT_APPROVAL_TITLE,
  type GeolocationApproval,
  type ScreenshotApproval,
} from "./approval_state";
import { CodeBlock } from "./code_block";
import { Field } from "./field";
import { CameraIcon, ChecklistIcon, ClockIcon, CloseIcon, HomeIcon, MapPinIcon, SettingsIcon } from "./icons";
import type { ConsoleNavigationView, ConsoleView } from "./routing";
import { SUCCESS_COLOR } from "./theme";
import { formatTimestamp } from "./time";
import { useVariant, VariantControl } from "./variant_control";
import { RelativeTime } from "../../../util/frontend/relative_time";

export interface ShellChromeProps {
  // Capability request panel open-state, parent-controlled so a new bridge request can
  // force it open.
  capabilityRequestsOpen: boolean;
  onCapabilityRequestsOpenChange: (open: boolean) => void;
  geolocationApprovals: GeolocationApproval[];
  screenshotApprovals: ScreenshotApproval[];
  decidingCapabilityRequestIds: readonly string[];
  onApproveGeolocation: (approval: GeolocationApproval) => void;
  onDenyGeolocation: (approval: GeolocationApproval) => void;
  onApproveScreenshot: (approval: ScreenshotApproval) => void;
  onDenyScreenshot: (approval: ScreenshotApproval) => void;
  view: ConsoleView;
  onNavigate: (view: ConsoleNavigationView) => void;
  // Location-sharing chrome (rendered only while the standing grant is held): a map-pin toggle
  // with a live indicator while a watch is actively reading, opening a stop/withdraw panel.
  geoGranted: boolean;
  tracking: boolean;
  onWithdrawGeolocation: () => void;
  // Screenshot chrome (rendered only while the standing grant is held): a camera toggle with a
  // live indicator while the shell holds an active tab-capture stream, opening a stop/withdraw
  // panel. `sharing` mirrors ScreenshotSession.active, not "a capture is in flight" — capture
  // itself is instant once sharing.
  screenshotGranted: boolean;
  sharingScreen: boolean;
  onWithdrawScreenshot: () => void;
  // The current session's absolute deadline (null until `/auth/me` answers). The rail surfaces it
  // only once it is close, so the operator can re-authenticate deliberately instead of being
  // bounced to Authentik by whichever background request happens to fail first.
  sessionExpiresAt: Date | null;
  sessionExpiringSoon: boolean;
  onReauthenticate: () => void;
}

// zIndex maxed so the shell chrome (toolbar + panels) sits above the full-page iframe.
export const PANEL_Z = 2147483647;
const ARM_DELAY_MS = 400;

function useArmed(identity: string | null): boolean {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!identity) {
      setArmed(false);
      return;
    }
    setArmed(false);
    const t = window.setTimeout(() => setArmed(true), ARM_DELAY_MS);
    return () => window.clearTimeout(t);
  }, [identity]);
  return armed;
}

// The location-sharing panel: opened from the map-pin toggle, shown only while the shell holds the
// standing grant. Carries the current state and the stop/withdraw kill switch, and occupies the
// shell's one shared panel slot like capability requests and screenshot capture.
function LocationPanel({ tracking, onWithdraw }: { tracking: boolean; onWithdraw: () => void }) {
  return (
    <section className="haku-shell-card haku-shell-side-panel" aria-label="Location sharing">
      <Stack gap="xs">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Text fw={600} size="sm">
            Location sharing
          </Text>
          <Badge color={tracking ? "teal" : "blue"} variant="light">
            {tracking ? "Live" : "Allowed"}
          </Badge>
        </Group>
        <Text size="xs" c="dimmed">
          {tracking
            ? "Haku's UI is reading your location right now."
            : "Haku's UI may read your location until you withdraw consent."}
        </Text>
        <Button size="compact-sm" variant="light" color="red" fullWidth onClick={onWithdraw}>
          {tracking ? "Stop & withdraw" : "Withdraw"}
        </Button>
      </Stack>
    </section>
  );
}

// The screenshot chrome panel: opened from the camera toggle, shown only while the shell holds
// the standing grant. "Live" means the shell currently holds an active tab-capture stream (the
// browser's own sharing indicator is up); "Allowed" means the operator has granted the
// capability but nothing is being captured right now — the next request will re-open the
// browser's own share picker.
function ScreenshotPanel({ sharing, onWithdraw }: { sharing: boolean; onWithdraw: () => void }) {
  return (
    <section className="haku-shell-card haku-shell-side-panel" aria-label="Screenshot capture">
      <Stack gap="xs">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Text fw={600} size="sm">
            Screenshot capture
          </Text>
          <Badge color={sharing ? "teal" : "blue"} variant="light">
            {sharing ? "Live" : "Allowed"}
          </Badge>
        </Group>
        <Text size="xs" c="dimmed">
          {sharing
            ? "Your browser is currently sharing this tab so Haku's UI can request screenshots."
            : "Haku's UI may ask to capture a screenshot until you withdraw consent."}
        </Text>
        <Button size="compact-sm" variant="light" color="red" fullWidth onClick={onWithdraw}>
          {sharing ? "Stop & withdraw" : "Withdraw"}
        </Button>
      </Stack>
    </section>
  );
}

// The session panel: opened from the hourglass toggle, which appears only once the operator
// session is close to its absolute deadline. The console cannot extend that deadline, so the
// panel's job is to make re-authentication a deliberate gesture — a tab that runs out mid-task
// navigates itself to Authentik and takes the framed Haku UI's unsaved state with it.
function SessionPanel({ expiresAt, onReauthenticate }: { expiresAt: Date; onReauthenticate: () => void }) {
  return (
    <section className="haku-shell-card haku-shell-side-panel" aria-label="Console session">
      <Stack gap="xs">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Text fw={600} size="sm">
            Console session
          </Text>
          <Badge color="orange" variant="light">
            Ends <RelativeTime value={expiresAt} format={formatTimestamp} />
          </Badge>
        </Group>
        <Text size="xs" c="dimmed">
          Console sessions last an hour and cannot be extended. Re-authenticating reloads this tab — anything unsaved in
          Haku&apos;s UI is lost either way, so pick a good moment.
        </Text>
        <Button size="compact-sm" variant="light" color="orange" fullWidth onClick={onReauthenticate}>
          Re-authenticate now
        </Button>
      </Stack>
    </section>
  );
}

function GeolocationApprovalCard({
  approval,
  deciding,
  onApprove,
  onDeny,
}: {
  approval: GeolocationApproval;
  deciding: boolean;
  onApprove: () => void;
  onDeny: () => void;
}) {
  const [variant, setVariant] = useVariant("compact");
  const armed = useArmed(`geo-card:${approval.id}`);
  const detailed = variant === "detailed";
  return (
    <section className="haku-shell-card">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
          <Stack gap={2} style={{ minWidth: 0 }}>
            <Text fw={600} size="sm">
              {geolocationApprovalTitle(approval)}
            </Text>
            <Text size="xs" c="dimmed">
              {approval.mode === "geolocationWatch" ? "Continuous location watch" : "Current location read"}
            </Text>
            <Text size="xs">{geolocationApprovalBody(approval)}</Text>
          </Stack>
          <Group gap="xs" align="center" wrap="nowrap" style={{ flexShrink: 0 }}>
            <Badge color={deciding ? "blue" : "yellow"} variant="light">
              {deciding ? "Applying" : "Pending"}
            </Badge>
            <VariantControl variant={variant} onChange={setVariant} />
          </Group>
        </Group>
        {detailed && (
          <div className="haku-shell-fields">
            <Field label="Requested">
              <RelativeTime value={new Date(approval.createdAt)} format={formatTimestamp} />
            </Field>
            {approval.options && (
              <Field label="Options">
                <CodeBlock language="json" value={JSON.stringify(approval.options, null, 2)} />
              </Field>
            )}
            <details className="haku-shell-disclosure">
              <summary>Agent UI bridge request ID</summary>
              <code>{approval.id}</code>
            </details>
          </div>
        )}
        <Group justify="flex-end" gap="xs">
          <Button size="compact-sm" variant="light" color="red" disabled={deciding || !armed} onClick={onDeny}>
            Deny
          </Button>
          <Button size="compact-sm" color={SUCCESS_COLOR} disabled={deciding || !armed} onClick={onApprove}>
            Approve
          </Button>
        </Group>
      </Stack>
    </section>
  );
}

function ScreenshotApprovalCard({
  approval,
  deciding,
  onApprove,
  onDeny,
}: {
  approval: ScreenshotApproval;
  deciding: boolean;
  onApprove: () => void;
  onDeny: () => void;
}) {
  const armed = useArmed(`shot-card:${approval.id}`);
  return (
    <section className="haku-shell-card">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
          <Stack gap={2} style={{ minWidth: 0 }}>
            <Text fw={600} size="sm">
              {SCREENSHOT_APPROVAL_TITLE}
            </Text>
            <Text size="xs">{SCREENSHOT_APPROVAL_BODY}</Text>
            <Text size="xs" c="dimmed">
              Requested <RelativeTime value={new Date(approval.createdAt)} format={formatTimestamp} />
            </Text>
          </Stack>
          <Badge color={deciding ? "blue" : "yellow"} variant="light" style={{ flexShrink: 0 }}>
            {deciding ? "Applying" : "Pending"}
          </Badge>
        </Group>
        <Group justify="flex-end" gap="xs">
          <Button size="compact-sm" variant="light" color="red" disabled={deciding || !armed} onClick={onDeny}>
            Deny
          </Button>
          <Button size="compact-sm" color={SUCCESS_COLOR} disabled={deciding || !armed} onClick={onApprove}>
            Approve
          </Button>
        </Group>
      </Stack>
    </section>
  );
}

export function CapabilityRequests({
  geolocationApprovals,
  screenshotApprovals,
  decidingCapabilityRequestIds,
  onApproveGeolocation,
  onDenyGeolocation,
  onApproveScreenshot,
  onDenyScreenshot,
}: Pick<
  ShellChromeProps,
  | "geolocationApprovals"
  | "screenshotApprovals"
  | "decidingCapabilityRequestIds"
  | "onApproveGeolocation"
  | "onDenyGeolocation"
  | "onApproveScreenshot"
  | "onDenyScreenshot"
>): JSX.Element {
  const items = useMemo(
    () => capabilityRequestItems(geolocationApprovals, screenshotApprovals),
    [geolocationApprovals, screenshotApprovals]
  );
  const deciding = new Set(decidingCapabilityRequestIds);

  return (
    <Stack gap="md">
      {items.length === 0 && (
        <Text size="sm" c="dimmed">
          No capability requests pending.
        </Text>
      )}
      {items.map((item) => {
        if (item.kind === "geolocation") {
          return (
            <GeolocationApprovalCard
              key={item.id}
              approval={item.approval}
              deciding={deciding.has(item.id)}
              onApprove={() => onApproveGeolocation(item.approval)}
              onDeny={() => onDenyGeolocation(item.approval)}
            />
          );
        }
        return (
          <ScreenshotApprovalCard
            key={item.id}
            approval={item.approval}
            deciding={deciding.has(item.id)}
            onApprove={() => onApproveScreenshot(item.approval)}
            onDeny={() => onDenyScreenshot(item.approval)}
          />
        );
      })}
    </Stack>
  );
}

// The capability request panel occupies the shared panel slot below the
// toolbar, follows its content up to the available height, and then scrolls its own list.
function CapabilityRequestsPanel(props: ShellChromeProps & { onClose: () => void }) {
  const pendingCount = props.geolocationApprovals.length + props.screenshotApprovals.length;
  return (
    <aside className="haku-shell-card haku-shell-capability-requests" aria-label="Capability requests">
      <section className="haku-shell-panel-nav">
        <Group justify="space-between" align="center" wrap="nowrap" className="haku-shell-header">
          <Group gap="xs" align="center">
            <Text fw={700}>Capability requests</Text>
            <Badge color={pendingCount > 0 ? "yellow" : "gray"} variant="light">
              {pendingCount}
            </Badge>
          </Group>
          <ActionIcon variant="subtle" color="gray" aria-label="Close capability requests" onClick={props.onClose}>
            <CloseIcon />
          </ActionIcon>
        </Group>
      </section>
      <div className="haku-shell-scroll">
        <CapabilityRequests {...props} />
      </div>
    </aside>
  );
}

function RailButton({
  open,
  label,
  color = "gray",
  onClick,
  children,
}: {
  open: boolean;
  label: string;
  color?: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <Tooltip label={label} position="right" withArrow openDelay={350} zIndex={PANEL_Z}>
      <ActionIcon
        size="lg"
        radius="md"
        variant={open ? "filled" : "subtle"}
        color={color}
        onClick={onClick}
        aria-label={label}
        aria-pressed={open}
      >
        {children}
      </ActionIcon>
    </Tooltip>
  );
}

export type IndicatorPanel = "location" | "screenshot" | "session";

// Trusted chrome lives in a real left-hand layout rail. Page navigation is independent from
// bridge capability requests, while the compact indicator popovers are mutually exclusive.
export function ShellChrome(props: ShellChromeProps): JSX.Element {
  const {
    capabilityRequestsOpen,
    onCapabilityRequestsOpenChange,
    geoGranted,
    tracking,
    onWithdrawGeolocation,
    screenshotGranted,
    sharingScreen,
    onWithdrawScreenshot,
    sessionExpiresAt,
    sessionExpiringSoon,
    onReauthenticate,
  } = props;
  const [openIndicator, setOpenIndicator] = useState<IndicatorPanel | null>(null);
  const pendingCount = props.geolocationApprovals.length + props.screenshotApprovals.length;

  useEffect(() => {
    if (
      (openIndicator === "location" && !geoGranted) ||
      (openIndicator === "screenshot" && !screenshotGranted) ||
      (openIndicator === "session" && !sessionExpiringSoon)
    ) {
      setOpenIndicator(null);
    }
  }, [geoGranted, openIndicator, screenshotGranted, sessionExpiringSoon]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (openIndicator) setOpenIndicator(null);
      else if (capabilityRequestsOpen) onCapabilityRequestsOpenChange(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [capabilityRequestsOpen, onCapabilityRequestsOpenChange, openIndicator]);

  const toggleIndicator = (panel: IndicatorPanel) => setOpenIndicator((open) => (open === panel ? null : panel));

  return (
    <>
      <nav className="haku-shell-rail" aria-label="Haku Console" style={{ zIndex: PANEL_Z }}>
        <div className="haku-shell-rail-top">
          <Indicator color="blue" size={16} offset={5} disabled={pendingCount === 0} label={pendingCount || undefined}>
            <RailButton
              open={capabilityRequestsOpen}
              label={capabilityRequestsOpen ? "Close capability requests" : "Open capability requests"}
              onClick={() => onCapabilityRequestsOpenChange(!capabilityRequestsOpen)}
            >
              <ChecklistIcon />
            </RailButton>
          </Indicator>
          <div className="haku-shell-rail-divider" />
          <RailButton open={props.view === "embed"} label="Haku UI" onClick={() => props.onNavigate("embed")}>
            <HomeIcon />
          </RailButton>
          <RailButton open={props.view === "settings"} label="Settings" onClick={() => props.onNavigate("settings")}>
            <SettingsIcon />
          </RailButton>
        </div>
        <div className="haku-shell-rail-bottom">
          {geoGranted && (
            <Indicator color="green" size={10} offset={5} processing disabled={!tracking} withBorder>
              <RailButton
                open={openIndicator === "location"}
                label={tracking ? "Location sharing: live" : "Location sharing: allowed"}
                onClick={() => toggleIndicator("location")}
              >
                <MapPinIcon />
              </RailButton>
            </Indicator>
          )}
          {sessionExpiringSoon && sessionExpiresAt !== null && (
            <RailButton
              open={openIndicator === "session"}
              label="Session expiring soon"
              color="orange"
              onClick={() => toggleIndicator("session")}
            >
              <ClockIcon />
            </RailButton>
          )}
          {screenshotGranted && (
            <Indicator color="green" size={10} offset={5} processing disabled={!sharingScreen} withBorder>
              <RailButton
                open={openIndicator === "screenshot"}
                label={sharingScreen ? "Screenshot capture: live" : "Screenshot capture: allowed"}
                onClick={() => toggleIndicator("screenshot")}
              >
                <CameraIcon />
              </RailButton>
            </Indicator>
          )}
        </div>
        {openIndicator && (
          <div className="haku-shell-indicator-popover">
            {openIndicator === "location" && geoGranted && (
              <LocationPanel tracking={tracking} onWithdraw={onWithdrawGeolocation} />
            )}
            {openIndicator === "screenshot" && screenshotGranted && (
              <ScreenshotPanel sharing={sharingScreen} onWithdraw={onWithdrawScreenshot} />
            )}
            {openIndicator === "session" && sessionExpiresAt !== null && (
              <SessionPanel expiresAt={sessionExpiresAt} onReauthenticate={onReauthenticate} />
            )}
          </div>
        )}
      </nav>
      {capabilityRequestsOpen && (
        <div className="haku-shell-drawer haku-shell-panels" style={{ zIndex: PANEL_Z - 1 }}>
          <CapabilityRequestsPanel {...props} onClose={() => onCapabilityRequestsOpenChange(false)} />
        </div>
      )}
    </>
  );
}
