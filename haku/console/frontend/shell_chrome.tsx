import { ActionIcon, Badge, Button, Group, Indicator, Loader, Stack, Text, Tooltip } from "@mantine/core";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

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
} from "./approval_state";
import type { ToolCallRecord } from "./client";
import { CodeBlock } from "./code_block";
import { Field } from "./field";
import {
  CameraIcon,
  ChecklistIcon,
  ClockIcon,
  CloseIcon,
  HistoryIcon,
  HomeIcon,
  MapPinIcon,
  SettingsIcon,
  SyncCurrentIcon,
  SyncErrorIcon,
} from "./icons";
import { PendingToolCallActions } from "./pending_tool_call_actions";
import type { ConsoleNavigationView, ConsoleView } from "./routing";
import { SUCCESS_COLOR } from "./theme";
import { ToolCallCard } from "./tool_call_card";
import type { LiveStatus } from "./console_events";
import { useVariant, VariantControl } from "./variant_control";

export interface ShellChromeProps {
  // Approvals panel open-state, parent-controlled so a newly-arrived geolocation approval can
  // force it open.
  approvalsOpen: boolean;
  focusedToolCallId?: string | null;
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
  view: ConsoleView;
  onNavigate: (view: ConsoleNavigationView) => void;
  // Live tool-call WebSocket health: drives the sync-status icon that is always visible (as an
  // ok-sync indicator) and the clickable panel that explains the current state.
  liveStatus: LiveStatus;
  // Error message from the last REST fetch of pending approvals, or null on success. Shown in
  // the sync-status panel and turns the icon orange so transient load errors are visible without
  // a toast flood.
  syncError: string | null;
  syncing: boolean;
  lastSyncAt: Date | null;
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

// The session panel: opened from the hourglass toggle, which appears only once the operator
// session is close to its absolute deadline. The console cannot extend that deadline, so the
// panel's job is to make re-authentication a deliberate gesture — a tab that runs out mid-task
// navigates itself to Authentik and takes the framed Haku UI's unsaved state with it.
function SessionPanel({ expiresAt, onReauthenticate }: { expiresAt: Date; onReauthenticate: () => void }) {
  const expiry = formatTimestamp(expiresAt.toISOString());
  return (
    <section className="haku-shell-card haku-shell-side-panel" aria-label="Console session">
      <Stack gap="xs">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Text fw={600} size="sm">
            Console session
          </Text>
          <Badge color="orange" variant="light" title={expiry.title}>
            Ends {expiry.text}
          </Badge>
        </Group>
        <Text size="xs" c="dimmed">
          Console sessions last an hour and cannot be extended. Re-authenticating reloads this tab — anything unsaved in
          Haku's UI is lost either way, so pick a good moment.
        </Text>
        <Button size="compact-sm" variant="light" color="orange" fullWidth onClick={onReauthenticate}>
          Re-authenticate now
        </Button>
      </Stack>
    </section>
  );
}

// Sync-status panel: always accessible via the wifi icon in the toolbar. Shows the current
// state of the live WebSocket channel and the last REST fetch outcome in one place, so the
// operator never has to wonder whether the approval list is current.
export type SyncState = "current" | "syncing" | "error";

// Exhaustive over LiveStatus with no fallback: the old if-chain ended in `return "current"`, so a
// new live-channel state would have been reported as fully healthy — the one answer that must
// never be a default here, since the whole point of this indicator is to say when the channel is
// broken. The maps below are keyed for the same reason.
export function syncState(liveStatus: LiveStatus, syncError: string | null, syncing: boolean): SyncState {
  if (syncError !== null) return "error";
  switch (liveStatus) {
    case "offline":
      return "error";
    case "connecting":
      return "syncing";
    case "live":
      return syncing ? "syncing" : "current";
  }
}

// Also the sync rail button's aria-labels, which the screenshot scenes select on.
const SYNC_STATE_LABEL: Record<SyncState, string> = {
  error: "Sync error",
  syncing: "Syncing",
  current: "Up to date",
};

const SYNC_BADGE_COLOR: Record<SyncState, string> = { error: "orange", syncing: "yellow", current: "teal" };

// The rail leaves `syncing` uncolored so the inline Loader carries that state on its own.
const SYNC_RAIL_COLOR: Record<SyncState, string | undefined> = {
  error: "orange",
  syncing: undefined,
  current: "teal",
};

function SyncStatusPanel({
  liveStatus,
  syncError,
  syncing,
  lastSyncAt,
}: {
  liveStatus: LiveStatus;
  syncError: string | null;
  syncing: boolean;
  lastSyncAt: Date | null;
}) {
  const offline = liveStatus === "offline";
  const state = syncState(liveStatus, syncError, syncing);
  return (
    <section className="haku-shell-card haku-shell-side-panel" aria-label="Sync status">
      <Stack gap="xs">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Text fw={600} size="sm">
            Sync status
          </Text>
          <Badge color={SYNC_BADGE_COLOR[state]} variant="light">
            {/* Not SYNC_STATE_LABEL: this badge splits `error` into Offline vs Fetch error, a
                distinction SyncState deliberately collapses. */}
            {offline ? "Offline" : syncError !== null ? "Fetch error" : SYNC_STATE_LABEL[state]}
          </Badge>
        </Group>
        {offline && (
          <Text size="xs" c="dimmed">
            The console lost its live connection, so new tool calls and approvals won't appear on their own. It keeps
            retrying; reload the page to refresh immediately.
          </Text>
        )}
        {!offline && syncError !== null && (
          <Text size="xs" c="dimmed">
            Failed to load pending approvals: {syncError}
          </Text>
        )}
        {state === "syncing" && !offline && syncError === null && (
          <Text size="xs" c="dimmed">
            {liveStatus === "connecting" ? "Connecting to live updates…" : "Refreshing pending approvals…"}
          </Text>
        )}
        {state === "current" && (
          <Text size="xs" c="dimmed">
            Live updates are connected and pending approvals are current.
          </Text>
        )}
        {lastSyncAt && (
          <Text size="xs" c="dimmed">
            Last refreshed{" "}
            {lastSyncAt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}
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
  focused = false,
}: {
  approval: ToolCallRecord;
  deciding: boolean;
  onApprove: () => void;
  onDeny: (reason?: string) => void;
  /** This card is the one a deep link (a push notification's Details, or the URL the MCP server
   * advertised) named: it opens expanded and scrolls itself into view. */
  focused?: boolean;
}) {
  // A deep-linked call opens detailed, because the operator followed a one-line notification here
  // precisely to see the arguments they could not see there.
  const [variant, setVariant] = useVariant(focused ? "detailed" : "compact");
  const cardRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (focused) cardRef.current?.scrollIntoView({ block: "center" });
  }, [focused]);
  const fields = approvalDisplayFields(approval);
  const armed = useArmed(`card:${approval.tool_call_id}`);
  return (
    <ToolCallCard
      fields={fields}
      args={approval.arguments}
      variant={variant}
      onVariantChange={setVariant}
      containerRef={cardRef}
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
  focusedToolCallId,
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
> & { focusedToolCallId?: string | null }) {
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
                  focused={item.approval.tool_call_id === focusedToolCallId}
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
function ApprovalsPanel(props: ShellChromeProps & { onClose: () => void }) {
  const pendingCount =
    props.pendingApprovals.length + props.geolocationApprovals.length + props.screenshotApprovals.length;
  return (
    <aside className="haku-shell-card haku-shell-approvals" aria-label="Approvals">
      <section className="haku-shell-panel-nav">
        <Group justify="space-between" align="center" wrap="nowrap" className="haku-shell-header">
          <Group gap="xs" align="center">
            <Text fw={700}>Approvals</Text>
            <Badge color={pendingCount > 0 ? "yellow" : "gray"} variant="light">
              {pendingCount}
            </Badge>
          </Group>
          <ActionIcon variant="subtle" color="gray" aria-label="Close approvals" onClick={props.onClose}>
            <CloseIcon />
          </ActionIcon>
        </Group>
        <Button
          size="xs"
          variant="light"
          color="gray"
          fullWidth
          leftSection={<HistoryIcon />}
          onClick={() => props.onNavigate("toolCalls")}
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

export type IndicatorPanel = "location" | "screenshot" | "sync-status" | "session";

// Trusted chrome lives in a real left-hand layout rail. Page navigation is independent from the
// approvals drawer, while the three compact indicator popovers are mutually exclusive.
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
    sessionExpiresAt,
    sessionExpiringSoon,
    onReauthenticate,
  } = props;
  const [openIndicator, setOpenIndicator] = useState<IndicatorPanel | null>(null);
  const pendingCount =
    props.pendingApprovals.length + props.geolocationApprovals.length + props.screenshotApprovals.length;
  const currentSyncState = syncState(liveStatus, syncError, props.syncing);

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
      else if (approvalsOpen) onApprovalsOpenChange(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [approvalsOpen, onApprovalsOpenChange, openIndicator]);

  const toggleIndicator = (panel: IndicatorPanel) => setOpenIndicator((open) => (open === panel ? null : panel));

  return (
    <>
      <nav className="haku-shell-rail" aria-label="Haku Console" style={{ zIndex: PANEL_Z }}>
        <div className="haku-shell-rail-top">
          <Indicator
            color="red"
            size={16}
            offset={5}
            processing
            disabled={pendingCount === 0}
            label={pendingCount || undefined}
          >
            <RailButton
              open={approvalsOpen}
              label={approvalsOpen ? "Close approvals" : "Open approvals"}
              onClick={() => onApprovalsOpenChange(!approvalsOpen)}
            >
              <ChecklistIcon />
            </RailButton>
          </Indicator>
          <div className="haku-shell-rail-divider" />
          <RailButton open={props.view === "embed"} label="Haku UI" onClick={() => props.onNavigate("embed")}>
            <HomeIcon />
          </RailButton>
          <RailButton
            open={props.view === "settings" || props.view === "agentEnrollment"}
            label="Settings"
            onClick={() => props.onNavigate("settings")}
          >
            <SettingsIcon />
          </RailButton>
          <RailButton
            open={props.view === "toolCalls"}
            label="Past tool calls"
            onClick={() => props.onNavigate("toolCalls")}
          >
            <HistoryIcon />
          </RailButton>
        </div>
        <div className="haku-shell-rail-bottom">
          <RailButton
            open={openIndicator === "sync-status"}
            label={SYNC_STATE_LABEL[currentSyncState]}
            color={SYNC_RAIL_COLOR[currentSyncState]}
            onClick={() => toggleIndicator("sync-status")}
          >
            {currentSyncState === "error" ? (
              <SyncErrorIcon />
            ) : currentSyncState === "syncing" ? (
              <Loader size={20} color="gray" />
            ) : (
              <SyncCurrentIcon />
            )}
          </RailButton>
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
            {openIndicator === "sync-status" && (
              <SyncStatusPanel
                liveStatus={liveStatus}
                syncError={syncError}
                syncing={props.syncing}
                lastSyncAt={props.lastSyncAt}
              />
            )}
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
      {approvalsOpen && (
        <div className="haku-shell-drawer haku-shell-panels" style={{ zIndex: PANEL_Z - 1 }}>
          <ApprovalsPanel {...props} onClose={() => onApprovalsOpenChange(false)} />
        </div>
      )}
    </>
  );
}
