import type { GeolocationOptions } from "@haku/console-bridge/protocol";

import type { ToolCallRecord } from "./client";

export interface GeolocationApproval {
  id: string;
  mode: "geolocation" | "geolocationWatch";
  options?: GeolocationOptions;
  createdAt: string;
}

export interface ScreenshotApproval {
  id: string;
  createdAt: string;
}

interface ApprovalQueueItemBase {
  id: string;
  createdAt: string;
}

export type ApprovalQueueItem =
  | (ApprovalQueueItemBase & { kind: "tool"; approval: ToolCallRecord })
  | (ApprovalQueueItemBase & { kind: "geolocation"; approval: GeolocationApproval })
  | (ApprovalQueueItemBase & { kind: "screenshot"; approval: ScreenshotApproval });

export interface ApprovalDisplayFields {
  title: string;
  serverId: string;
  toolName: string;
  rationale: string;
  argumentsJson: string;
  toolCallId: string;
  callerDisplayName: string;
  callerAgentId: string | null;
  createdAt: string | null;
  denialReason: string | null;
  withdrawalReason: string | null;
  approvalPolicyId: string | null;
  autoApprovalEvaluation: string | null;
}

export interface RecentToolCall {
  record: ToolCallRecord;
  hideAtMs: number;
}

export interface RecentToolCallCountdown {
  label: string;
  progressPercent: number;
  remainingSeconds: number;
}

const RECENT_OK_TTL_MS = 15_000;
const RECENT_ERROR_TTL_MS = 60_000;

function createdAtMs(value: string | undefined): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function toolApprovalQueueId(toolCallId: string): string {
  return `tool:${toolCallId}`;
}

export function geolocationApprovalQueueId(id: string): string {
  return `geolocation:${id}`;
}

export function screenshotApprovalQueueId(id: string): string {
  return `screenshot:${id}`;
}

export function approvalQueueItems(
  toolApprovals: readonly ToolCallRecord[],
  geolocationApprovals: readonly GeolocationApproval[],
  screenshotApprovals: readonly ScreenshotApproval[]
): ApprovalQueueItem[] {
  return [
    ...toolApprovals.map(
      (approval): ApprovalQueueItem => ({
        kind: "tool",
        id: toolApprovalQueueId(approval.tool_call_id),
        createdAt: approval.created_at,
        approval,
      })
    ),
    ...geolocationApprovals.map(
      (approval): ApprovalQueueItem => ({
        kind: "geolocation",
        id: geolocationApprovalQueueId(approval.id),
        createdAt: approval.createdAt,
        approval,
      })
    ),
    ...screenshotApprovals.map(
      (approval): ApprovalQueueItem => ({
        kind: "screenshot",
        id: screenshotApprovalQueueId(approval.id),
        createdAt: approval.createdAt,
        approval,
      })
    ),
  ].sort((a, b) => createdAtMs(b.createdAt) - createdAtMs(a.createdAt));
}

export function approvalDisplayFields(approval: ToolCallRecord): ApprovalDisplayFields {
  const args = approval.arguments ?? {};
  const toolName = approval.tool_name ?? "unknown tool";
  return {
    title: approval.title ?? `${approval.server_id}: ${toolName}`,
    serverId: approval.server_id,
    toolName,
    rationale: approval.rationale ?? "",
    argumentsJson: JSON.stringify(args, null, 2) ?? "null",
    toolCallId: approval.tool_call_id,
    callerDisplayName: approval.caller.kind === "agent" ? approval.caller.display_name : "Operator",
    callerAgentId: approval.caller.kind === "agent" ? approval.caller.agent_id : null,
    createdAt: approval.created_at ?? null,
    denialReason: approval.denial_reason ?? null,
    withdrawalReason: approval.withdrawal_reason ?? null,
    approvalPolicyId: approval.approval_policy_id ?? null,
    autoApprovalEvaluation: approval.auto_approval_evaluation ?? null,
  };
}

// The "Recent" list is fed only by the operator's own decisions (`finishToolDecision` in
// haku_ui_embed.tsx), so `withdrawn` — an agent retraction — never enters it and gets no TTL.
export function recentToolCallTtlMs(status: ToolCallRecord["status"]): number | null {
  if (status === "ok" || status === "denied") return RECENT_OK_TTL_MS;
  if (status === "error") return RECENT_ERROR_TTL_MS;
  return null;
}

export function makeRecentToolCall(record: ToolCallRecord, nowMs: number): RecentToolCall | null {
  const ttlMs = recentToolCallTtlMs(record.status);
  return ttlMs === null ? null : { record, hideAtMs: nowMs + ttlMs };
}

export function recentToolCallCountdown(recent: RecentToolCall, nowMs: number): RecentToolCallCountdown {
  const ttlMs = recentToolCallTtlMs(recent.record.status) ?? 0;
  const remainingMs = Math.max(0, recent.hideAtMs - nowMs);
  const remainingSeconds = Math.ceil(remainingMs / 1000);
  const progressPercent = ttlMs === 0 ? 0 : Math.max(0, Math.min(100, (remainingMs / ttlMs) * 100));
  return {
    label: remainingSeconds === 0 ? "Auto-hides now" : `Auto-hides in ${remainingSeconds}s`,
    progressPercent,
    remainingSeconds,
  };
}

// Keyed maps rather than if-chains with a fallback: a new status is then a type error here instead
// of silently rendering as a blue "Pending" badge.
const STATUS_LABELS: Record<ToolCallRecord["status"], string> = {
  pending_approval: "Pending",
  running: "Running",
  ok: "OK",
  error: "Error",
  denied: "Denied",
  withdrawn: "Withdrawn",
};

const STATUS_COLORS: Record<ToolCallRecord["status"], string> = {
  // Approval is an operator decision, so keep it distinct from a running call and bright enough
  // to read against the dark shell background.
  pending_approval: "orange",
  running: "blue",
  ok: "teal",
  error: "red",
  denied: "gray",
  withdrawn: "gray",
};

/** Whether the card should show the auto-approval evaluation string.
 *
 * A policy that matched says what let the call through unattended, which earns a compact line. One
 * that did not only explains an absence the operator is already looking at, so it is provenance and
 * compact omits it. Detailed carries it either way. */
export function showsAutoApprovalEvaluation(
  fields: Pick<ApprovalDisplayFields, "autoApprovalEvaluation" | "approvalPolicyId">,
  detailed: boolean
): boolean {
  if (!fields.autoApprovalEvaluation) return false;
  return detailed || fields.approvalPolicyId !== null;
}

export function terminalStatusLabel(status: ToolCallRecord["status"]): string {
  return STATUS_LABELS[status];
}

export function statusColor(status: ToolCallRecord["status"]): string {
  return STATUS_COLORS[status];
}

export function geolocationApprovalTitle(approval: GeolocationApproval): string {
  return approval.mode === "geolocationWatch" ? "Allow continuous location sharing?" : "Allow location sharing?";
}

export function geolocationApprovalBody(approval: GeolocationApproval): string {
  return approval.mode === "geolocationWatch"
    ? "Haku is asking to continuously receive your device location until it stops the watch or you withdraw access."
    : "Haku is asking to read your current device location once.";
}

// Same wording for a first grant and for a resume after the operator stopped sharing: either way
// approving opens the browser's own tab-share picker.
export const SCREENSHOT_APPROVAL_TITLE = "Allow screen capture for a screenshot?";
export const SCREENSHOT_APPROVAL_BODY: string =
  "Haku is asking to capture a screenshot of this page. Approving opens your browser's own " +
  "share-this-tab picker; once granted, further screenshots are instant until you withdraw or " +
  "stop sharing.";
