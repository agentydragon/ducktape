import { ActionIcon, Badge, Button, Group, Indicator, Popover, Stack, Text, Textarea } from "@mantine/core";
import type { KeyboardEvent } from "react";
import { useEffect, useMemo, useState } from "react";

import {
  approvalDisplayFields,
  approvalQueueItems,
  geolocationApprovalBody,
  geolocationApprovalTitle,
  recentToolCallCountdown,
  shortDate,
  statusColor,
  type GeolocationApproval,
  type RecentToolCall,
  terminalStatusLabel,
} from "./approval_state.ts";
import type { PendingApproval } from "./client.ts";
import { Field } from "./field.tsx";
import { HistoryIcon, MapPinIcon, MenuIcon, SettingsIcon } from "./icons.tsx";
import { ACTION_COLOR } from "./theme.ts";
import { ToolArgumentsField } from "./tool_arguments_field.tsx";

export interface ShellDrawerProps {
  opened: boolean;
  onClose: () => void;
  pendingApprovals: PendingApproval[];
  geolocationApprovals: GeolocationApproval[];
  selectedApprovalId: string | null;
  selectedRecentToolCallId: string | null;
  decidingApprovalIds: readonly string[];
  recentToolCalls: RecentToolCall[];
  onSelectApproval: (id: string) => void;
  onSelectRecentToolCall: (toolCallId: string) => void;
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

function openOnCardKeyDown(e: KeyboardEvent<HTMLElement>, onOpen: () => void) {
  if (e.key !== "Enter" && e.key !== " ") return;
  e.preventDefault();
  onOpen();
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

function ToolApprovalDetail({
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
  const fields = approvalDisplayFields(approval);
  const armed = useArmed(`detail:${approval.tool_call_id}`);
  const [denyReason, setDenyReason] = useState("");
  useEffect(() => {
    setDenyReason("");
  }, [approval.tool_call_id]);
  return (
    <section className="haku-shell-card haku-shell-card-selected">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" gap="sm">
          <Stack gap={3}>
            <Text fw={700}>{fields.title}</Text>
            <Text size="xs" c="dimmed">
              Tool call approval
            </Text>
          </Stack>
          <Badge color={deciding ? "blue" : "yellow"} variant="light">
            {deciding ? "Running" : "Pending"}
          </Badge>
        </Group>
        <dl className="haku-shell-fields">
          <div className="haku-shell-field-grid">
            <Field label="Server id" mono>
              {fields.serverId}
            </Field>
            <Field label="Tool name" mono>
              {fields.toolName}
            </Field>
          </div>
          <Field label="Rationale">{fields.rationale || "No rationale provided."}</Field>
          <ToolArgumentsField
            serverId={fields.serverId}
            toolName={fields.toolName}
            args={approval.arguments}
            argumentsJson={fields.argumentsJson}
          />
          {(fields.callerPrincipal || fields.createdAt) && (
            <Field label="Requested">
              {[fields.callerPrincipal, shortDate(fields.createdAt)].filter(Boolean).join(" · ")}
            </Field>
          )}
          <details className="haku-shell-disclosure">
            <summary>Tool call id</summary>
            <code>{fields.toolCallId}</code>
          </details>
        </dl>
        <Textarea
          size="xs"
          label="Denial reason (optional)"
          placeholder="Why are you denying this?"
          autosize
          minRows={1}
          maxRows={4}
          disabled={deciding}
          value={denyReason}
          onChange={(e) => setDenyReason(e.currentTarget.value)}
        />
        <Group justify="space-between">
          <Button
            size="sm"
            variant="light"
            color="red"
            disabled={deciding || !armed}
            onClick={() => onDeny(denyReason.trim() || undefined)}
          >
            Deny
          </Button>
          <Button size="sm" color={ACTION_COLOR} disabled={deciding || !armed} onClick={onApprove}>
            Approve
          </Button>
        </Group>
      </Stack>
    </section>
  );
}

function GeolocationApprovalDetail({
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
  const armed = useArmed(`geo-detail:${approval.id}`);
  return (
    <section className="haku-shell-card haku-shell-card-selected">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" gap="sm">
          <Stack gap={3}>
            <Text fw={700}>{geolocationApprovalTitle(approval)}</Text>
            <Text size="xs" c="dimmed">
              Browser location approval
            </Text>
          </Stack>
          <Badge color={deciding ? "blue" : "yellow"} variant="light">
            {deciding ? "Applying" : "Pending"}
          </Badge>
        </Group>
        <Text size="sm">{geolocationApprovalBody(approval)}</Text>
        <dl className="haku-shell-fields">
          <Field label="Mode">{approval.mode === "geolocationWatch" ? "Continuous watch" : "One-shot read"}</Field>
          <Field label="Requested">{shortDate(approval.createdAt)}</Field>
          {approval.options && (
            <Field label="Options">
              <pre className="haku-shell-json">{JSON.stringify(approval.options, null, 2)}</pre>
            </Field>
          )}
          <details className="haku-shell-disclosure">
            <summary>Bridge request id</summary>
            <code>{approval.id}</code>
          </details>
        </dl>
        <Group justify="space-between">
          <Button size="sm" variant="light" color="red" disabled={deciding || !armed} onClick={onDeny}>
            Deny
          </Button>
          <Button size="sm" color={ACTION_COLOR} disabled={deciding || !armed} onClick={onApprove}>
            Approve
          </Button>
        </Group>
      </Stack>
    </section>
  );
}

function ToolApprovalCard({
  approval,
  selected,
  deciding,
  onSelect,
  onApprove,
  onDeny,
}: {
  approval: PendingApproval;
  selected: boolean;
  deciding: boolean;
  onSelect: () => void;
  onApprove: () => void;
  onDeny: (reason?: string) => void;
}) {
  const fields = approvalDisplayFields(approval);
  const armed = useArmed(`card:${approval.tool_call_id}`);
  // An always-visible optional reason field so a single Deny click can carry a "why"
  // straight from the card, without opening the detail view.
  const [denyReason, setDenyReason] = useState("");
  return (
    <section
      className={`haku-shell-card haku-shell-approval-card ${selected ? "haku-shell-card-active" : ""}`}
      onClick={onSelect}
    >
      <div
        className="haku-shell-card-click-target"
        role="button"
        tabIndex={0}
        onClick={(e) => {
          e.stopPropagation();
          onSelect();
        }}
        onKeyDown={(e) => openOnCardKeyDown(e, onSelect)}
      >
        <Group justify="space-between" align="flex-start" gap="sm">
          <Stack gap={2}>
            <Text fw={600} size="sm">
              {fields.title}
            </Text>
            <Text size="xs" c="dimmed">
              {fields.serverId}.{fields.toolName}
            </Text>
            <Text size="xs">{fields.rationale || fields.argumentSummary}</Text>
          </Stack>
          <Badge color={deciding ? "blue" : "yellow"} variant="light">
            {deciding ? "Running" : "Pending"}
          </Badge>
        </Group>
      </div>
      {/* When selected, the detail panel above carries the reason field + actions, so the
          list card collapses to a highlighted summary — no duplicate reason input. */}
      {!selected && (
        <div onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
          <Textarea
            size="xs"
            mt="xs"
            label="Denial reason (optional)"
            placeholder="Why are you denying this?"
            autosize
            minRows={1}
            maxRows={4}
            disabled={deciding}
            value={denyReason}
            onChange={(e) => setDenyReason(e.currentTarget.value)}
          />
          <Group justify="flex-end" gap="xs" mt="xs">
            <Button
              size="compact-xs"
              variant="light"
              color="red"
              disabled={deciding || !armed}
              onClick={() => onDeny(denyReason.trim() || undefined)}
            >
              Deny
            </Button>
            <Button size="compact-xs" color={ACTION_COLOR} disabled={deciding || !armed} onClick={onApprove}>
              Approve
            </Button>
          </Group>
        </div>
      )}
    </section>
  );
}

function GeolocationApprovalCard({
  approval,
  selected,
  deciding,
  onSelect,
  onApprove,
  onDeny,
}: {
  approval: GeolocationApproval;
  selected: boolean;
  deciding: boolean;
  onSelect: () => void;
  onApprove: () => void;
  onDeny: () => void;
}) {
  const armed = useArmed(`geo-card:${approval.id}`);
  return (
    <section
      className={`haku-shell-card haku-shell-approval-card ${selected ? "haku-shell-card-active" : ""}`}
      onClick={onSelect}
    >
      <div
        className="haku-shell-card-click-target"
        role="button"
        tabIndex={0}
        onClick={(e) => {
          e.stopPropagation();
          onSelect();
        }}
        onKeyDown={(e) => openOnCardKeyDown(e, onSelect)}
      >
        <Group justify="space-between" align="flex-start" gap="sm">
          <Stack gap={2}>
            <Text fw={600} size="sm">
              {geolocationApprovalTitle(approval)}
            </Text>
            <Text size="xs" c="dimmed">
              {approval.mode === "geolocationWatch" ? "Continuous location watch" : "Current location read"}
            </Text>
            <Text size="xs">{geolocationApprovalBody(approval)}</Text>
          </Stack>
          <Badge color={deciding ? "blue" : "yellow"} variant="light">
            {deciding ? "Applying" : "Pending"}
          </Badge>
        </Group>
      </div>
      <Group
        justify="flex-end"
        gap="xs"
        mt="xs"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.stopPropagation()}
      >
        <Button size="compact-xs" variant="light" color="red" disabled={deciding || !armed} onClick={onDeny}>
          Deny
        </Button>
        <Button size="compact-xs" color={ACTION_COLOR} disabled={deciding || !armed} onClick={onApprove}>
          Approve
        </Button>
      </Group>
    </section>
  );
}

function RecentToolCallCard({
  recent,
  selected,
  nowMs,
  onSelect,
  onDismiss,
}: {
  recent: RecentToolCall;
  selected: boolean;
  nowMs: number;
  onSelect: () => void;
  onDismiss: () => void;
}) {
  const fields = approvalDisplayFields(recent.record);
  return (
    <section
      className={`haku-shell-card haku-shell-approval-card ${selected ? "haku-shell-card-active" : ""}`}
      onClick={onSelect}
    >
      <div
        className="haku-shell-card-click-target"
        role="button"
        tabIndex={0}
        onClick={(e) => {
          e.stopPropagation();
          onSelect();
        }}
        onKeyDown={(e) => openOnCardKeyDown(e, onSelect)}
      >
        <Group justify="space-between" align="flex-start" gap="sm">
          <Stack gap={2}>
            <Text fw={600} size="sm">
              {fields.title}
            </Text>
            <Text size="xs" c="dimmed">
              {fields.serverId}.{fields.toolName}
            </Text>
            {recent.record.error && (
              <Text size="xs" c="red">
                {recent.record.error}
              </Text>
            )}
            {fields.denialReason && (
              <Text size="xs" c="dimmed">
                Denied: {fields.denialReason}
              </Text>
            )}
          </Stack>
          <Badge color={statusColor(recent.record.status)} variant="light">
            {terminalStatusLabel(recent.record.status)}
          </Badge>
        </Group>
        <RecentCountdown recent={recent} nowMs={nowMs} />
      </div>
      <Group
        justify="flex-end"
        gap="xs"
        mt="xs"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.stopPropagation()}
      >
        <Button size="compact-xs" variant="subtle" color="gray" onClick={onDismiss}>
          Dismiss
        </Button>
      </Group>
    </section>
  );
}

function RecentToolCallDetail({ recent, nowMs }: { recent: RecentToolCall; nowMs: number }) {
  const record = recent.record;
  const fields = approvalDisplayFields(record);
  return (
    <section className="haku-shell-card haku-shell-card-selected">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" gap="sm">
          <Stack gap={3}>
            <Text fw={700}>{fields.title}</Text>
            <Text size="xs" c="dimmed">
              Tool call result
            </Text>
          </Stack>
          <Badge color={statusColor(record.status)} variant="light">
            {terminalStatusLabel(record.status)}
          </Badge>
        </Group>
        <RecentCountdown recent={recent} nowMs={nowMs} />
        {record.error && (
          <Text size="sm" c="red">
            {record.error}
          </Text>
        )}
        <dl className="haku-shell-fields">
          <div className="haku-shell-field-grid">
            <Field label="Server id" mono>
              {fields.serverId}
            </Field>
            <Field label="Tool name" mono>
              {fields.toolName}
            </Field>
          </div>
          {fields.denialReason && <Field label="Denial reason">{fields.denialReason}</Field>}
          <ToolArgumentsField
            serverId={fields.serverId}
            toolName={fields.toolName}
            args={record.arguments}
            argumentsJson={fields.argumentsJson}
          />
          {record.result && (
            <Field label="Result">
              <pre className="haku-shell-json">{JSON.stringify(record.result, null, 2)}</pre>
            </Field>
          )}
          <details className="haku-shell-disclosure">
            <summary>Tool call id</summary>
            <code>{fields.toolCallId}</code>
          </details>
        </dl>
      </Stack>
    </section>
  );
}

function ApprovalsTab({
  pendingApprovals,
  geolocationApprovals,
  selectedApprovalId,
  selectedRecentToolCallId,
  decidingApprovalIds,
  recentToolCalls,
  onSelectApproval,
  onSelectRecentToolCall,
  onApproveTool,
  onDenyTool,
  onApproveGeolocation,
  onDenyGeolocation,
  onDismissRecentToolCall,
}: Pick<
  ShellDrawerProps,
  | "pendingApprovals"
  | "geolocationApprovals"
  | "selectedApprovalId"
  | "selectedRecentToolCallId"
  | "decidingApprovalIds"
  | "recentToolCalls"
  | "onSelectApproval"
  | "onSelectRecentToolCall"
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
  const selectedItem = items.find((item) => item.id === selectedApprovalId) ?? null;
  const selectedRecent =
    selectedItem === null
      ? recentToolCalls.find((recent) => recent.record.tool_call_id === selectedRecentToolCallId)
      : null;
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
      {selectedItem?.kind === "tool" && (
        <ToolApprovalDetail
          approval={selectedItem.approval}
          deciding={deciding.has(selectedItem.id)}
          onApprove={() => onApproveTool(selectedItem.approval)}
          onDeny={(reason) => onDenyTool(selectedItem.approval, reason)}
        />
      )}
      {selectedItem?.kind === "geolocation" && (
        <GeolocationApprovalDetail
          approval={selectedItem.approval}
          deciding={deciding.has(selectedItem.id)}
          onApprove={() => onApproveGeolocation(selectedItem.approval)}
          onDeny={() => onDenyGeolocation(selectedItem.approval)}
        />
      )}
      {selectedRecent && <RecentToolCallDetail recent={selectedRecent} nowMs={nowMs} />}
      {!selectedItem && !selectedRecent && !hasPending && !hasRecent && (
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
                selected={item.id === selectedApprovalId}
                deciding={deciding.has(item.id)}
                onSelect={() => onSelectApproval(item.id)}
                onApprove={() => onApproveTool(item.approval)}
                onDeny={(reason) => onDenyTool(item.approval, reason)}
              />
            ) : (
              <GeolocationApprovalCard
                key={item.id}
                approval={item.approval}
                selected={item.id === selectedApprovalId}
                deciding={deciding.has(item.id)}
                onSelect={() => onSelectApproval(item.id)}
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
              selected={recent.record.tool_call_id === selectedRecentToolCallId}
              nowMs={nowMs}
              onSelect={() => onSelectRecentToolCall(recent.record.tool_call_id)}
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
            <Group justify="space-between" align="center" className="haku-shell-header">
              <Group gap="xs" align="center">
                <Text fw={700}>Approvals</Text>
                <Badge color={pendingCount > 0 ? "yellow" : "gray"} variant="light">
                  {pendingCount}
                </Badge>
              </Group>
              <Button size="compact-xs" variant="subtle" onClick={props.onClose}>
                Close
              </Button>
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
