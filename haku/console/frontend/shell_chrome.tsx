import { ActionIcon, Badge, Button, Group, Indicator, Stack, Text } from "@mantine/core";
import { type ReactNode, useEffect, useMemo, useState } from "react";

import {
  approvalDisplayFields,
  approvalQueueItems,
  formatTimestamp,
  geolocationApprovalBody,
  geolocationApprovalTitle,
  recentToolCallCountdown,
  SCREENSHOT_APPROVAL_BODY,
  SCREENSHOT_APPROVAL_TITLE,
  statusColor,
  type GeolocationApproval,
  type RecentToolCall,
  type ScreenshotApproval,
  terminalStatusLabel,
} from "./approval_state.ts";
import type { ToolCallRecord } from "./client.ts";
import { CodeBlock } from "./code_block.tsx";
import { Field } from "./field.tsx";
import { CameraIcon, ChecklistIcon, HistoryIcon, MapPinIcon, SettingsIcon, WifiIcon, WifiOffIcon } from "./icons.tsx";
import { PendingToolCallActions } from "./pending_tool_call_actions.tsx";
import { SettingsPanel } from "./settings_panel.tsx";
import { SUCCESS_COLOR } from "./theme.ts";
import { ToolCallCard } from "./tool_call_card.tsx";
import type { LiveStatus } from "./console_events.ts";
import { useVariant, VariantControl } from "./variant_control.tsx";

export interface ShellChromeProps {
  // Approvals panel open-state, parent-controlled so a newly-arrived geolocation approval can
  // force it open.
  approvalsOpen: boolean;
  onApprovalsOpenChange: (open: boolean) => void;
  pendingApprovals: ToolCallRecord[];
  geolocationApprovals: GeolocationApproval[];
  screenshotApprovals: ScreenshotApproval[];
  decidingApprovalIds: readonly string[];
  recentToolCalls: RecentToolCall[];
  onApproveTool: (approval: ToolCallRecord) => void;
  onDenyTool: (approval: ToolCallRecord, reason?: string) => void;
  onApproveGeolocation: (approval: GeolocationApproval) => void;
  onDenyGeolocation: (approval: GeolocationApproval) => void;
  onApproveScreenshot: (approval: ScreenshotApproval) => void;
  onDenyScreenshot: (approval: ScreenshotApproval) => void;
  onDismissRecentToolCall: (toolCallId: string) => void;
  onOpenToolCalls: () => void;
  // Live tool-call WebSocket health: drives the sync-status icon that is always visible (as an
  // ok-sync indicator) and the clickable panel that explains the current state.
  liveStatus: LiveStatus;
  // Whether the last REST fetch of pending approvals failed. Shown in the sync-status panel
  // and turns the icon orange, so transient load errors are visible without a toast flood.
  syncError: boolean;
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

function RecentCountdown({ recent, nowMs }: { recent: RecentToolCall; nowMs: number }) {
  const countdown = recentToolCallCountdown(recent, nowMs);
  return (
    <div className="haku-shell-countdown" aria-label={countdown.label}>
      <Text size="xs" c="dimmed">
        {countdown.label}
      </Text>
      <div className="haku-shell-countdown-track" aria-hidden="true">
        <div className="haku-shell-countdown-fill" style={{ width: `${countdown.progressPercent}%` }} />
      </div>
    </div>
  );
}

// The location-sharing panel: opened from the map-pin toggle, shown only while the shell holds
// the standing grant. Carries the current state and the stop/withdraw kill switch (a rare
// one-tap action best reachable straight from the chrome). It occupies the shell's one shared
// panel slot, just like approvals, screenshot capture, and settings.
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

// Sync-status panel: always accessible via the wifi icon in the toolbar. Shows the current
// state of the live WebSocket channel and the last REST fetch outcome in one place, so the
// operator never has to wonder whether the approval list is current.
function SyncStatusPanel({ liveStatus, syncError }: { liveStatus: LiveStatus; syncError: boolean }) {
  const offline = liveStatus === "offline";
  const unhealthy = offline || syncError;
  return (
    <section className="haku-shell-card haku-shell-side-panel" aria-label="Sync status">
      <Stack gap="xs">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Text fw={600} size="sm">
            Sync status
          </Text>
          <Badge color={unhealthy ? "orange" : liveStatus === "connecting" ? "yellow" : "teal"} variant="light">
            {offline ? "Offline" : liveStatus === "connecting" ? "Connecting" : syncError ? "Fetch error" : "Live"}
          </Badge>
        </Group>
        {offline && (
          <Text size="xs" c="dimmed">
            The console lost its live connection, so new tool calls and approvals won't appear on their own. It keeps
            retrying; reload the page to refresh immediately.
          </Text>
        )}
        {!offline && syncError && (
          <Text size="xs" c="dimmed">
            The last attempt to load pending approvals failed. The list may be stale; reload the page to retry
            immediately.
          </Text>
        )}
        {!unhealthy && (
          <Text size="xs" c="dimmed">
            Live updates are connected and the approval list loaded successfully.
          </Text>
        )}
      </Stack>
    </section>
  );
}

// Each queue card renders compact by default and expands **in place** via its own Details
// toggle (the same control as the history page) — no separate detail panel, no overlay. The
// compact form is the scannable summary; the detailed form adds the fields you only want when
// digging in. An always-visible denial-reason field keeps a single Deny click able to carry a
// "why" from either form.
function ToolApprovalCard({
  approval,
  deciding,
  onApprove,
  onDeny,
}: {
  approval: ToolCallRecord;
  deciding: boolean;
  onApprove: () => void;
  onDeny: (reason?: string) => void;
}) {
  const [variant, setVariant] = useVariant("compact");
  const fields = approvalDisplayFields(approval);
  const armed = useArmed(`card:${approval.tool_call_id}`);
  return (
    <ToolCallCard
      fields={fields}
      args={approval.arguments}
      variant={variant}
      onVariantChange={setVariant}
      status={{ label: deciding ? "Running" : "Pending", color: deciding ? "blue" : "yellow" }}
      footer={<PendingToolCallActions busy={deciding} armed={armed} onApprove={onApprove} onDeny={onDeny} />}
    />
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
  const requested = formatTimestamp(approval.createdAt);
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
              <span title={requested.title}>{requested.text}</span>
            </Field>
            {approval.options && (
              <Field label="Options">
                <CodeBlock language="json" value={JSON.stringify(approval.options, null, 2)} />
              </Field>
            )}
            <details className="haku-shell-disclosure">
              <summary>Bridge request id</summary>
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
  const requested = formatTimestamp(approval.createdAt);
  return (
    <section className="haku-shell-card">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
          <Stack gap={2} style={{ minWidth: 0 }}>
            <Text fw={600} size="sm">
              {SCREENSHOT_APPROVAL_TITLE}
            </Text>
            <Text size="xs">{SCREENSHOT_APPROVAL_BODY}</Text>
            <Text size="xs" c="dimmed" title={requested.title}>
              Requested {requested.text}
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

function RecentToolCallCard({
  recent,
  nowMs,
  onDismiss,
}: {
  recent: RecentToolCall;
  nowMs: number;
  onDismiss: () => void;
}) {
  const [variant, setVariant] = useVariant("compact");
  const record = recent.record;
  const fields = approvalDisplayFields(record);
  return (
    <ToolCallCard
      fields={fields}
      args={record.arguments}
      variant={variant}
      onVariantChange={setVariant}
      status={{ label: terminalStatusLabel(record.status), color: statusColor(record.status) }}
      error={record.error}
      result={record.result}
      footer={
        <>
          <RecentCountdown recent={recent} nowMs={nowMs} />
          <Group justify="flex-end" gap="xs">
            <Button size="compact-xs" variant="subtle" color="gray" onClick={onDismiss}>
              Dismiss
            </Button>
          </Group>
        </>
      }
    />
  );
}

function ApprovalsTab({
  pendingApprovals,
  geolocationApprovals,
  screenshotApprovals,
  decidingApprovalIds,
  recentToolCalls,
  onApproveTool,
  onDenyTool,
  onApproveGeolocation,
  onDenyGeolocation,
  onApproveScreenshot,
  onDenyScreenshot,
  onDismissRecentToolCall,
}: Pick<
  ShellChromeProps,
  | "pendingApprovals"
  | "geolocationApprovals"
  | "screenshotApprovals"
  | "decidingApprovalIds"
  | "recentToolCalls"
  | "onApproveTool"
  | "onDenyTool"
  | "onApproveGeolocation"
  | "onDenyGeolocation"
  | "onApproveScreenshot"
  | "onDenyScreenshot"
  | "onDismissRecentToolCall"
>) {
  const items = useMemo(
    () => approvalQueueItems(pendingApprovals, geolocationApprovals, screenshotApprovals),
    [geolocationApprovals, pendingApprovals, screenshotApprovals]
  );
  const deciding = new Set(decidingApprovalIds);
  const hasPending = items.length > 0;
  const hasRecent = recentToolCalls.length > 0;
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (recentToolCalls.length === 0) return;
    const t = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [recentToolCalls.length]);

  return (
    <Stack gap="md">
      {!hasPending && !hasRecent && (
        <Text size="sm" c="dimmed">
          No approvals pending.
        </Text>
      )}
      {hasPending && (
        <Stack gap="xs">
          <Text fw={600} size="sm">
            Pending
          </Text>
          {items.map((item) => {
            if (item.kind === "tool") {
              return (
                <ToolApprovalCard
                  key={item.id}
                  approval={item.approval}
                  deciding={deciding.has(item.id)}
                  onApprove={() => onApproveTool(item.approval)}
                  onDeny={(reason) => onDenyTool(item.approval, reason)}
                />
              );
            }
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
      )}
      {hasRecent && (
        <Stack gap="xs">
          <Text fw={600} size="sm">
            Recent
          </Text>
          {recentToolCalls.map((recent) => (
            <RecentToolCallCard
              key={recent.record.tool_call_id}
              recent={recent}
              nowMs={nowMs}
              onDismiss={() => onDismissRecentToolCall(recent.record.tool_call_id)}
            />
          ))}
        </Stack>
      )}
    </Stack>
  );
}

// The approvals panel — the primary chrome surface. It occupies the shared panel slot below the
// toolbar, follows its content up to the available height, and then scrolls its own list.
function ApprovalsPanel(props: ShellChromeProps) {
  const pendingCount =
    props.pendingApprovals.length + props.geolocationApprovals.length + props.screenshotApprovals.length;
  return (
    <aside className="haku-shell-card haku-shell-approvals" aria-label="Approvals">
      <section className="haku-shell-panel-nav">
        <Group gap="xs" align="center" className="haku-shell-header">
          <Text fw={700}>Approvals</Text>
          <Badge color={pendingCount > 0 ? "yellow" : "gray"} variant="light">
            {pendingCount}
          </Badge>
        </Group>
        <Button
          size="xs"
          variant="light"
          color="gray"
          fullWidth
          leftSection={<HistoryIcon />}
          onClick={props.onOpenToolCalls}
        >
          Past tool calls
        </Button>
      </section>
      <div className="haku-shell-scroll">
        <ApprovalsTab {...props} />
      </div>
    </aside>
  );
}

// One toolbar toggle: `filled` (neutral gray, unless a semantic color is given) while its panel
// is open, `subtle` otherwise — so the row of squished buttons reads as pressed/unpressed.
function ChromeToggle({
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
  );
}

export type ShellPanel = "approvals" | "settings" | "location" | "screenshot" | "sync-status";

export function nextShellPanel(selected: ShellPanel | null, clicked: ShellPanel): ShellPanel | null {
  return selected === clicked ? null : clicked;
}

export function selectedShellPanel(approvalsOpen: boolean, openPanel: ShellPanel | null): ShellPanel | null {
  // A newly arriving approval may light the badge, but must not preempt an explicit operator
  // selection—especially the location/screenshot kill-switch panels.
  return openPanel ?? (approvalsOpen ? "approvals" : null);
}

// The persistent shell chrome over the framed haku-ui: a floating top-right toolbar whose
// mutually-exclusive toggles behave like deselectable tabs. Toggles are neutral gray; only
// genuinely semantic cues keep a color (red pending count, orange offline/error, green
// live-location/live-capture dot).
export function ShellChrome(props: ShellChromeProps) {
  const {
    approvalsOpen,
    onApprovalsOpenChange,
    liveStatus,
    syncError,
    geoGranted,
    tracking,
    onWithdrawGeolocation,
    screenshotGranted,
    sharingScreen,
    onWithdrawScreenshot,
  } = props;
  const [openPanel, setOpenPanel] = useState<ShellPanel | null>(null);
  const pendingCount =
    props.pendingApprovals.length + props.geolocationApprovals.length + props.screenshotApprovals.length;
  const offline = liveStatus === "offline";
  const syncUnhealthy = offline || syncError;
  const selectedPanel = selectedShellPanel(approvalsOpen, openPanel);

  useEffect(() => {
    if ((openPanel === "location" && !geoGranted) || (openPanel === "screenshot" && !screenshotGranted)) {
      setOpenPanel(null);
    }
  }, [geoGranted, openPanel, screenshotGranted]);

  function togglePanel(panel: ShellPanel) {
    const next = nextShellPanel(selectedPanel, panel);
    onApprovalsOpenChange(next === "approvals");
    setOpenPanel(next === "approvals" ? null : next);
  }

  return (
    <div className="haku-shell-chrome" style={{ zIndex: PANEL_Z }}>
      <Group className="haku-shell-toolbar" gap={0} wrap="nowrap">
        <ChromeToggle
          open={selectedPanel === "sync-status"}
          label={
            syncUnhealthy
              ? "Live updates: error"
              : liveStatus === "connecting"
                ? "Live updates: connecting"
                : "Live updates: connected"
          }
          color={syncUnhealthy ? "orange" : undefined}
          onClick={() => togglePanel("sync-status")}
        >
          {syncUnhealthy ? <WifiOffIcon /> : <WifiIcon />}
        </ChromeToggle>
        {geoGranted && (
          <Indicator color="green" size={10} offset={6} processing disabled={!tracking} withBorder>
            <ChromeToggle
              open={selectedPanel === "location"}
              label={tracking ? "Location sharing: live" : "Location sharing: allowed"}
              onClick={() => togglePanel("location")}
            >
              <MapPinIcon />
            </ChromeToggle>
          </Indicator>
        )}
        {screenshotGranted && (
          <Indicator color="green" size={10} offset={6} processing disabled={!sharingScreen} withBorder>
            <ChromeToggle
              open={selectedPanel === "screenshot"}
              label={sharingScreen ? "Screenshot capture: live" : "Screenshot capture: allowed"}
              onClick={() => togglePanel("screenshot")}
            >
              <CameraIcon />
            </ChromeToggle>
          </Indicator>
        )}
        <ChromeToggle open={selectedPanel === "settings"} label="Settings" onClick={() => togglePanel("settings")}>
          <SettingsIcon />
        </ChromeToggle>
        <Indicator
          color="red"
          size={16}
          offset={6}
          processing
          disabled={pendingCount === 0}
          label={pendingCount > 0 ? pendingCount : undefined}
        >
          <ChromeToggle
            open={selectedPanel === "approvals"}
            label={selectedPanel === "approvals" ? "Close approvals" : "Open approvals"}
            onClick={() => togglePanel("approvals")}
          >
            <ChecklistIcon />
          </ChromeToggle>
        </Indicator>
      </Group>
      {selectedPanel && (
        <div className="haku-shell-panels">
          {selectedPanel === "approvals" && <ApprovalsPanel {...props} />}
          {selectedPanel === "settings" && <SettingsPanel />}
          {selectedPanel === "location" && geoGranted && (
            <LocationPanel tracking={tracking} onWithdraw={onWithdrawGeolocation} />
          )}
          {selectedPanel === "screenshot" && screenshotGranted && (
            <ScreenshotPanel sharing={sharingScreen} onWithdraw={onWithdrawScreenshot} />
          )}
          {selectedPanel === "sync-status" && <SyncStatusPanel liveStatus={liveStatus} syncError={syncError} />}
        </div>
      )}
    </div>
  );
}
