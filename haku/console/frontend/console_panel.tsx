import { ActionIcon, Badge, Button, Group, Indicator, Popover, Stack, Text } from "@mantine/core";
import { useEffect, useMemo, useState } from "react";

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
import { HistoryIcon, MapPinIcon, MenuIcon, SettingsIcon } from "./icons.tsx";
import { PendingToolCallActions } from "./pending_tool_call_actions.tsx";
import { ACTION_COLOR, SUCCESS_COLOR } from "./theme.ts";
import { ToolCallCard } from "./tool_call_card.tsx";
import { useVariant, VariantToggle } from "./variant_toggle.tsx";

export interface ShellDrawerProps {
  opened: boolean;
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
  onOpenSettings: () => void;
}

export interface ShellControlsProps {
  pendingCount: number;
  opened: boolean;
  onToggle: () => void;
  // Location-sharing control (rendered only when the standing grant is held): the map-pin
  // popover under the hamburger, with a live indicator while a watch is actively reading.
  geoGranted: boolean;
  tracking: boolean;
  onWithdrawGeolocation: () => void;
}

// zIndex maxed so shell chrome sits above the full-page iframe; controls are one below.
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

// The location-sharing control: a map-pin under the hamburger, shown only while the shell
// holds the standing grant. A pulsing teal indicator dot marks that a watch is *actively*
// reading location right now (`tracking`); the plain pin means sharing is merely allowed.
// Its popover carries the state and the stop/withdraw kill switch (out of the drawer, since
// it's a rare one-tap action best reachable straight from the chrome).
function LocationControl({ tracking, onWithdraw }: { tracking: boolean; onWithdraw: () => void }) {
  const [opened, setOpened] = useState(false);
  return (
    <Popover opened={opened} onChange={setOpened} position="left" withArrow shadow="md" width={248}>
      <Popover.Target>
        <Indicator color="teal" size={10} offset={4} processing disabled={!tracking} withBorder>
          <ActionIcon
            size="lg"
            variant="default"
            color={ACTION_COLOR}
            onClick={() => setOpened((o) => !o)}
            aria-label={tracking ? "Location sharing: live" : "Location sharing: allowed"}
          >
            <MapPinIcon />
          </ActionIcon>
        </Indicator>
      </Popover.Target>
      <Popover.Dropdown>
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
          <Button
            size="compact-sm"
            variant="light"
            color="red"
            fullWidth
            onClick={() => {
              onWithdraw();
              setOpened(false);
            }}
          >
            {tracking ? "Stop & withdraw" : "Withdraw"}
          </Button>
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}

// The persistent shell chrome over the framed haku-ui, stacked top-right: a generic panel
// toggle (its pending-approval count surfaces as a pulsing red callout — a "something needs
// you" light, without spelling the drawer's contents onto the button) and, when location is
// shared, the location-sharing pin below it.
export function ShellControls({
  pendingCount,
  opened,
  onToggle,
  geoGranted,
  tracking,
  onWithdrawGeolocation,
}: ShellControlsProps) {
  return (
    <Stack className="haku-shell-controls" gap="xs" align="flex-end" style={{ zIndex: PANEL_Z - 1 }}>
      <Indicator
        color="red"
        size={16}
        offset={4}
        processing
        disabled={pendingCount === 0}
        label={pendingCount > 0 ? pendingCount : undefined}
      >
        <ActionIcon
          size="lg"
          variant={opened ? "filled" : "default"}
          color={ACTION_COLOR}
          onClick={onToggle}
          aria-label={opened ? "Close console panel" : "Open console panel"}
        >
          <MenuIcon />
        </ActionIcon>
      </Indicator>
      {geoGranted && <LocationControl tracking={tracking} onWithdraw={onWithdrawGeolocation} />}
    </Stack>
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
  const [variant, toggleVariant] = useVariant("compact");
  const fields = approvalDisplayFields(approval);
  const armed = useArmed(`card:${approval.tool_call_id}`);
  return (
    <ToolCallCard
      fields={fields}
      args={approval.arguments}
      variant={variant}
      onToggle={toggleVariant}
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
  const [variant, toggleVariant] = useVariant("compact");
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
          <Badge color={deciding ? "blue" : "yellow"} variant="light" style={{ flexShrink: 0 }}>
            {deciding ? "Applying" : "Pending"}
          </Badge>
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
        <VariantToggle variant={variant} onToggle={toggleVariant} />
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
  const [variant, toggleVariant] = useVariant("compact");
  const record = recent.record;
  const fields = approvalDisplayFields(record);
  return (
    <ToolCallCard
      fields={fields}
      args={record.arguments}
      variant={variant}
      onToggle={toggleVariant}
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
  ShellDrawerProps,
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

export function ShellDrawer(props: ShellDrawerProps) {
  const pendingCount = props.pendingApprovals.length + props.geolocationApprovals.length;
  return (
    <div className="haku-shell-overlay" style={{ zIndex: PANEL_Z }} aria-hidden={!props.opened}>
      {props.opened && (
        <aside className="haku-shell-drawer" aria-label="Haku console controls">
          <section className="haku-shell-card haku-shell-drawer-nav">
            <Group align="center" className="haku-shell-header">
              <Group gap="xs" align="center">
                <Text fw={700}>Approvals</Text>
                <Badge color={pendingCount > 0 ? "yellow" : "gray"} variant="light">
                  {pendingCount}
                </Badge>
              </Group>
            </Group>
            <Group grow gap="xs">
              <Button
                size="xs"
                variant="light"
                color={ACTION_COLOR}
                leftSection={<HistoryIcon />}
                onClick={props.onOpenToolCalls}
              >
                Past tool calls
              </Button>
              <Button
                size="xs"
                variant="light"
                color={ACTION_COLOR}
                leftSection={<SettingsIcon />}
                onClick={props.onOpenSettings}
              >
                Settings
              </Button>
            </Group>
          </section>
          <div className="haku-shell-scroll">
            <ApprovalsTab {...props} />
          </div>
        </aside>
      )}
    </div>
  );
}
