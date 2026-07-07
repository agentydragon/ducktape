import { Badge, Button, Group, SegmentedControl, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";

import {
  approvalDisplayFields,
  approvalQueueItems,
  formatHideCountdown,
  geolocationApprovalBody,
  geolocationApprovalTitle,
  type GeolocationApproval,
  type RecentToolCall,
  terminalStatusLabel,
} from "./approval_state.ts";
import type { McpOperatorAuthStatus, PendingApproval, ToolCallRecord } from "./client.ts";
import { ACTION_COLOR } from "./theme.ts";

export type ShellDrawerTab = "approvals" | "console";

export interface ShellDrawerProps {
  opened: boolean;
  activeTab: ShellDrawerTab;
  onOpenTab: (tab: ShellDrawerTab) => void;
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
  onDenyTool: (approval: PendingApproval) => void;
  onApproveGeolocation: (approval: GeolocationApproval) => void;
  onDenyGeolocation: (approval: GeolocationApproval) => void;
  onDismissRecentToolCall: (toolCallId: string) => void;
  geoGranted: boolean;
  tracking: boolean;
  onWithdrawGeolocation: () => void;
  mcpAuthStatuses: McpOperatorAuthStatus[];
  onConnectMcp: (serverId: string) => void;
  onDisconnectMcp: (serverId: string) => void;
  onRefreshMcp: () => void;
}

export interface ShellControlsProps {
  pendingCount: number;
  opened: boolean;
  activeTab: ShellDrawerTab;
  geoGranted: boolean;
  tracking: boolean;
  onOpenTab: (tab: ShellDrawerTab) => void;
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

function shortDate(value: string | null | undefined): string | null {
  if (!value) return null;
  return new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function statusColor(status: ToolCallRecord["status"]): string {
  if (status === "ok") return "teal";
  if (status === "error") return "red";
  if (status === "denied") return "gray";
  return "blue";
}

export function ShellControls({
  pendingCount,
  opened,
  activeTab,
  geoGranted,
  tracking,
  onOpenTab,
}: ShellControlsProps) {
  return (
    <Group className="haku-shell-controls" gap="xs" style={{ zIndex: PANEL_Z - 1 }}>
      <Button
        size="xs"
        variant={opened && activeTab === "approvals" ? "filled" : "default"}
        color={ACTION_COLOR}
        onClick={() => onOpenTab("approvals")}
        rightSection={
          pendingCount > 0 ? (
            <Badge size="xs" color="red" variant="filled">
              {pendingCount}
            </Badge>
          ) : null
        }
      >
        Approvals
      </Button>
      <Button
        size="xs"
        variant={opened && activeTab === "console" ? "filled" : "default"}
        color={tracking ? "teal" : geoGranted ? "blue" : "gray"}
        onClick={() => onOpenTab("console")}
      >
        Console
      </Button>
    </Group>
  );
}

function Field({ label, children, mono = false }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="haku-shell-field">
      <dt>{label}</dt>
      <dd className={mono ? "haku-shell-mono" : ""}>{children}</dd>
    </div>
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
  onDeny: () => void;
}) {
  const fields = approvalDisplayFields(approval);
  const armed = useArmed(`detail:${approval.tool_call_id}`);
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
          <Field label="Arguments">
            <pre className="haku-shell-json">{fields.argumentsJson}</pre>
          </Field>
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
  onDeny: () => void;
}) {
  const fields = approvalDisplayFields(approval);
  const armed = useArmed(`card:${approval.tool_call_id}`);
  return (
    <section className={`haku-shell-card haku-shell-approval-card ${selected ? "haku-shell-card-active" : ""}`}>
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
      <Group justify="flex-end" gap="xs" mt="xs">
        <Button size="compact-xs" variant="subtle" onClick={onSelect}>
          Details
        </Button>
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
    <section className={`haku-shell-card haku-shell-approval-card ${selected ? "haku-shell-card-active" : ""}`}>
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
      <Group justify="flex-end" gap="xs" mt="xs">
        <Button size="compact-xs" variant="subtle" onClick={onSelect}>
          Details
        </Button>
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
    <section className={`haku-shell-card haku-shell-approval-card ${selected ? "haku-shell-card-active" : ""}`}>
      <Group justify="space-between" align="flex-start" gap="sm">
        <Stack gap={2}>
          <Text fw={600} size="sm">
            {fields.title}
          </Text>
          <Text size="xs" c="dimmed">
            {fields.serverId}.{fields.toolName} · {formatHideCountdown(recent.hideAtMs, nowMs)}
          </Text>
          {recent.record.error && (
            <Text size="xs" c="red">
              {recent.record.error}
            </Text>
          )}
        </Stack>
        <Badge color={statusColor(recent.record.status)} variant="light">
          {terminalStatusLabel(recent.record.status)}
        </Badge>
      </Group>
      <Group justify="flex-end" gap="xs" mt="xs">
        <Button size="compact-xs" variant="subtle" onClick={onSelect}>
          Details
        </Button>
        <Button size="compact-xs" variant="subtle" color="gray" onClick={onDismiss}>
          Dismiss
        </Button>
      </Group>
    </section>
  );
}

function RecentToolCallDetail({ record }: { record: ToolCallRecord }) {
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
          <Field label="Arguments">
            <pre className="haku-shell-json">{fields.argumentsJson}</pre>
          </Field>
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
          onDeny={() => onDenyTool(selectedItem.approval)}
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
      {selectedRecent && <RecentToolCallDetail record={selectedRecent.record} />}
      {!selectedItem && !selectedRecent && (
        <section className="haku-shell-card">
          <Text size="sm" c="dimmed">
            No pending approvals.
          </Text>
        </section>
      )}
      <Stack gap="xs">
        <Text fw={600} size="sm">
          Pending
        </Text>
        {items.length === 0 ? (
          <Text size="sm" c="dimmed">
            Nothing needs a decision.
          </Text>
        ) : (
          items.map((item) =>
            item.kind === "tool" ? (
              <ToolApprovalCard
                key={item.id}
                approval={item.approval}
                selected={item.id === selectedApprovalId}
                deciding={deciding.has(item.id)}
                onSelect={() => onSelectApproval(item.id)}
                onApprove={() => onApproveTool(item.approval)}
                onDeny={() => onDenyTool(item.approval)}
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
          )
        )}
      </Stack>
      {recentToolCalls.length > 0 && (
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

function ConsoleTab({
  geoGranted,
  tracking,
  onWithdrawGeolocation,
  mcpAuthStatuses,
  onConnectMcp,
  onDisconnectMcp,
  onRefreshMcp,
}: Pick<
  ShellDrawerProps,
  | "geoGranted"
  | "tracking"
  | "onWithdrawGeolocation"
  | "mcpAuthStatuses"
  | "onConnectMcp"
  | "onDisconnectMcp"
  | "onRefreshMcp"
>) {
  return (
    <Stack gap="md">
      <section className="haku-shell-card">
        <Group justify="space-between" align="flex-start" gap="sm">
          <Stack gap={4}>
            <Text fw={600} size="sm">
              Location sharing
            </Text>
            <Text size="sm" c="dimmed">
              {geoGranted
                ? tracking
                  ? "Haku is receiving live location updates."
                  : "Haku may request your location without another approval until withdrawn."
                : "Not shared. Haku will request approval when it needs your location."}
            </Text>
          </Stack>
          <Badge color={tracking ? "teal" : geoGranted ? "blue" : "gray"} variant="light">
            {tracking ? "Live" : geoGranted ? "Allowed" : "Off"}
          </Badge>
        </Group>
        {geoGranted && (
          <Group justify="flex-end" mt="sm">
            <Button size="xs" variant="light" color="red" onClick={onWithdrawGeolocation}>
              {tracking ? "Stop & withdraw" : "Withdraw"}
            </Button>
          </Group>
        )}
      </section>
      <section className="haku-shell-card">
        <Group justify="space-between" align="center">
          <Text fw={600} size="sm">
            MCP accounts
          </Text>
          <Button size="compact-xs" variant="subtle" onClick={onRefreshMcp}>
            Refresh
          </Button>
        </Group>
        <Stack gap="sm" mt="sm">
          {mcpAuthStatuses.length === 0 ? (
            <Text size="sm" c="dimmed">
              No operator-linked MCP servers are configured.
            </Text>
          ) : (
            mcpAuthStatuses.map((status) => (
              <div key={status.server_id} className="haku-shell-subcard">
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
                  <Badge color={status.status === "connected" ? "teal" : "gray"} variant="light">
                    {status.status === "connected" ? "Connected" : "Unconnected"}
                  </Badge>
                </Group>
                <Group justify="flex-end" gap="xs" mt="xs">
                  {status.status === "connected" ? (
                    <>
                      <Button size="compact-xs" variant="light" onClick={() => onConnectMcp(status.server_id)}>
                        Reconnect
                      </Button>
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        color="red"
                        onClick={() => onDisconnectMcp(status.server_id)}
                      >
                        Disconnect
                      </Button>
                    </>
                  ) : (
                    <Button size="compact-xs" variant="light" onClick={() => onConnectMcp(status.server_id)}>
                      Connect
                    </Button>
                  )}
                </Group>
              </div>
            ))
          )}
        </Stack>
      </section>
    </Stack>
  );
}

export function ShellDrawer(props: ShellDrawerProps) {
  return (
    <div className="haku-shell-overlay" style={{ zIndex: PANEL_Z }} aria-hidden={!props.opened}>
      {props.opened && (
        <aside className="haku-shell-drawer" aria-label="Haku console controls">
          <Group justify="space-between" align="center" className="haku-shell-header">
            <Stack gap={1}>
              <Text fw={700}>Haku console</Text>
              <Text size="xs" c="dimmed">
                Trusted shell controls
              </Text>
            </Stack>
            <Button size="compact-xs" variant="subtle" onClick={props.onClose}>
              Close
            </Button>
          </Group>
          <SegmentedControl
            fullWidth
            size="xs"
            value={props.activeTab}
            onChange={(value) => props.onOpenTab(value as ShellDrawerTab)}
            data={[
              {
                value: "approvals",
                label: `Approvals (${props.pendingApprovals.length + props.geolocationApprovals.length})`,
              },
              { value: "console", label: "Console" },
            ]}
          />
          <div className="haku-shell-scroll">
            {props.activeTab === "approvals" ? (
              <ApprovalsTab {...props} />
            ) : (
              <ConsoleTab
                geoGranted={props.geoGranted}
                tracking={props.tracking}
                onWithdrawGeolocation={props.onWithdrawGeolocation}
                mcpAuthStatuses={props.mcpAuthStatuses}
                onConnectMcp={props.onConnectMcp}
                onDisconnectMcp={props.onDisconnectMcp}
                onRefreshMcp={props.onRefreshMcp}
              />
            )}
          </div>
        </aside>
      )}
    </div>
  );
}
