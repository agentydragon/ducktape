import { ActionIcon, Badge, Button, Group, Indicator, Stack, Text } from "@mantine/core";
import { type ReactNode, useEffect, useMemo, useState } from "react";

import {
  approvalDisplayFields,
  approvalQueueItems,
  formatTimestamp,
  geolocationApprovalBody,
  geolocationApprovalTitle,
  recentToolCallCountdown,
  statusColor,
  type GeolocationApproval,
  type RecentToolCall,
  terminalStatusLabel,
} from "./approval_state.ts";
import type { PendingApproval } from "./client.ts";
import { Field } from "./field.tsx";
import { ChecklistIcon, HistoryIcon, MapPinIcon, SettingsIcon, WifiOffIcon } from "./icons.tsx";
import { PendingToolCallActions } from "./pending_tool_call_actions.tsx";
import { SettingsPanel } from "./settings_page.tsx";
import { SUCCESS_COLOR } from "./theme.ts";
import { ToolCallCard } from "./tool_call_card.tsx";
import type { LiveStatus } from "./tool_call_events.ts";
import { useVariant, VariantControl } from "./variant_control.tsx";

export interface ShellChromeProps {
  // Approvals panel open-state, parent-controlled so a newly-arrived geolocation approval can
  // force it open.
  approvalsOpen: boolean;
  onApprovalsOpenChange: (open: boolean) => void;
  pendingApprovals: PendingApproval[];
  geolocationApprovals: GeolocationApproval[];
  decidingApprovalIds: readonly string[];
  recentToolCalls: RecentToolCall[];
  onApproveTool: (approval: PendingApproval) => void;
  onDenyTool: (approval: PendingApproval, reason?: string) => void;
  onApproveGeolocation: (approval: GeolocationApproval) => void;
  onDenyGeolocation: (approval: GeolocationApproval) => void;
  onDismissRecentToolCall: (toolCallId: string) => void;
  onOpenToolCalls: () => void;
  // Live tool-call WebSocket health: when `offline`, the chrome shows a toggle for a warning
  // panel so a dead channel (approvals only update on reload) is visible, not silent.
  liveStatus: LiveStatus;
  // Location-sharing chrome (rendered only while the standing grant is held): a map-pin toggle
  // with a live indicator while a watch is actively reading, opening a stop/withdraw panel.
  geoGranted: boolean;
  tracking: boolean;
  onWithdrawGeolocation: () => void;
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
// one-tap action best reachable straight from the chrome). Rendered as a stacked panel in the
// chrome column, not a floating popover, so it sits under its sibling panels by Y.
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

// Shown from the crossed-wifi toggle when the live tool-call WebSocket is down: explains that
// approvals may be stale until the connection recovers. Without this a broken live channel is
// invisible — the approvals list just silently stops updating between reloads (the exact
// failure the missing nginx WS upgrade caused). The socket auto-reconnects, so the toggle
// clears itself once it's back.
function LivePanel() {
  return (
    <section className="haku-shell-card haku-shell-side-panel" aria-label="Live updates offline">
      <Stack gap="xs">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Text fw={600} size="sm">
            Live updates offline
          </Text>
          <Badge color="orange" variant="light">
            Reconnecting
          </Badge>
        </Group>
        <Text size="xs" c="dimmed">
          The console lost its live connection, so new tool calls and approvals won't appear on their own. It keeps
          retrying; reload the page to refresh immediately.
        </Text>
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
  approval: PendingApproval;
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
          <Stack gap={6} align="flex-end" style={{ flexShrink: 0 }}>
            <Badge color={deciding ? "blue" : "yellow"} variant="light">
              {deciding ? "Applying" : "Pending"}
            </Badge>
            <VariantControl variant={variant} onChange={setVariant} />
          </Stack>
        </Group>
        {detailed && (
          <div className="haku-shell-fields">
            <Field label="Requested">
              <span title={requested.title}>{requested.text}</span>
            </Field>
            {approval.options && (
              <Field label="Options">
                <pre className="haku-shell-json">{JSON.stringify(approval.options, null, 2)}</pre>
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
  decidingApprovalIds,
  recentToolCalls,
  onApproveTool,
  onDenyTool,
  onApproveGeolocation,
  onDenyGeolocation,
  onDismissRecentToolCall,
}: Pick<
  ShellChromeProps,
  | "pendingApprovals"
  | "geolocationApprovals"
  | "decidingApprovalIds"
  | "recentToolCalls"
  | "onApproveTool"
  | "onDenyTool"
  | "onApproveGeolocation"
  | "onDenyGeolocation"
  | "onDismissRecentToolCall"
>) {
  const items = useMemo(
    () => approvalQueueItems(pendingApprovals, geolocationApprovals),
    [geolocationApprovals, pendingApprovals]
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
        <section className="haku-shell-card">
          <Text size="sm" c="dimmed">
            No approvals pending.
          </Text>
        </section>
      )}
      {hasPending && (
        <Stack gap="xs">
          <Text fw={600} size="sm">
            Pending
          </Text>
          {items.map((item) =>
            item.kind === "tool" ? (
              <ToolApprovalCard
                key={item.id}
                approval={item.approval}
                deciding={deciding.has(item.id)}
                onApprove={() => onApproveTool(item.approval)}
                onDeny={(reason) => onDenyTool(item.approval, reason)}
              />
            ) : (
              <GeolocationApprovalCard
                key={item.id}
                approval={item.approval}
                deciding={deciding.has(item.id)}
                onApprove={() => onApproveGeolocation(item.approval)}
                onDeny={() => onDenyGeolocation(item.approval)}
              />
            )
          )}
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

// The approvals panel — the primary chrome surface. One block in the chrome column (below the
// toolbar), it flexes to fill the column's height and scrolls its own list, so the smaller
// settings/location/live panels can stack beneath it rather than being covered.
function ApprovalsPanel(props: ShellChromeProps) {
  const pendingCount = props.pendingApprovals.length + props.geolocationApprovals.length;
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
    >
      {children}
    </ActionIcon>
  );
}

// The persistent shell chrome over the framed haku-ui: a floating top-right **toolbar** of
// toggle buttons (squished together, no gaps) over a column that stacks its open panels **by
// Y**, never by z-index. Each button is `filled` while its panel is open; opening more than one
// panel stacks them vertically under the toolbar — the approvals panel flexes to fill remaining
// height and scrolls internally, the smaller settings/location/live panels take their natural
// height beneath it — so panels share the column instead of floating over one another. Toggles
// are neutral gray; only genuinely semantic cues keep a color (red pending count, orange
// offline, green live-location dot).
export function ShellChrome(props: ShellChromeProps) {
  const { approvalsOpen, onApprovalsOpenChange, liveStatus, geoGranted, tracking, onWithdrawGeolocation } = props;
  const [locationOpen, setLocationOpen] = useState(false);
  const [liveOpen, setLiveOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const pendingCount = props.pendingApprovals.length + props.geolocationApprovals.length;
  const offline = liveStatus === "offline";
  const anyPanelOpen = approvalsOpen || settingsOpen || (geoGranted && locationOpen) || (offline && liveOpen);
  return (
    <div className="haku-shell-chrome" style={{ zIndex: PANEL_Z }}>
      <Group className="haku-shell-toolbar" gap={0} wrap="nowrap">
        {offline && (
          <ChromeToggle
            open={liveOpen}
            label="Live updates disconnected"
            color="orange"
            onClick={() => setLiveOpen((o) => !o)}
          >
            <WifiOffIcon />
          </ChromeToggle>
        )}
        {geoGranted && (
          <Indicator color="green" size={10} offset={6} processing disabled={!tracking} withBorder>
            <ChromeToggle
              open={locationOpen}
              label={tracking ? "Location sharing: live" : "Location sharing: allowed"}
              onClick={() => setLocationOpen((o) => !o)}
            >
              <MapPinIcon />
            </ChromeToggle>
          </Indicator>
        )}
        <ChromeToggle open={settingsOpen} label="Settings" onClick={() => setSettingsOpen((o) => !o)}>
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
            open={approvalsOpen}
            label={approvalsOpen ? "Close approvals" : "Open approvals"}
            onClick={() => onApprovalsOpenChange(!approvalsOpen)}
          >
            <ChecklistIcon />
          </ChromeToggle>
        </Indicator>
      </Group>
      {anyPanelOpen && (
        <div className="haku-shell-panels">
          {approvalsOpen && <ApprovalsPanel {...props} />}
          {settingsOpen && <SettingsPanel />}
          {geoGranted && locationOpen && <LocationPanel tracking={tracking} onWithdraw={onWithdrawGeolocation} />}
          {offline && liveOpen && <LivePanel />}
        </div>
      )}
    </div>
  );
}
