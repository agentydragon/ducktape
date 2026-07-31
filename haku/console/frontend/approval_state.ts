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
  pending_approval: "blue",
  running: "blue",
  ok: "teal",
  error: "red",
  denied: "gray",
  withdrawn: "gray",
};

/** Whether the card should show the auto-approval evaluation string.
 *
 * The evaluation records why the reviewed policy did or did not match. When it *did*, it says what
 * let the call through unattended, which is worth a compact line. When it did not, the string only
 * explains an absence — and that absence is the very reason the operator is looking at the card at
 * all — so it is provenance, which compact omits. Detailed still carries it either way. */
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

export function shortDate(value: string | null | undefined): string | null {
  if (!value) return null;
  return new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

const _ABS_DATETIME = new Intl.DateTimeFormat([], { dateStyle: "medium", timeStyle: "short" });
const _TIME = new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit" });
const _MONTH_DAY = new Intl.DateTimeFormat([], { month: "short", day: "numeric" });
const _MONTH_DAY_YEAR = new Intl.DateTimeFormat([], { month: "short", day: "numeric", year: "numeric" });

const _MINUTE = 60_000;
const _HOUR = 60 * _MINUTE;
const _DAY = 24 * _HOUR;

/** A timestamp in a concise, human-readable form (relative when near — "2h ago", "in 30 min",
 * "yesterday 2:32 PM" — a short date otherwise) with the full locale date+time as `title` for
 * the element's tooltip. Shared by every card's Requested/Metadata field so they read one way. */
export function formatTimestamp(value: string, nowMs: number = Date.now()): { text: string; title: string } {
  const d = new Date(value);
  const title = _ABS_DATETIME.format(d);
  const diffMs = d.getTime() - nowMs; // >0 future, <0 past
  const absMs = Math.abs(diffMs);
  const ago = (near: string): string => (diffMs < 0 ? `${near} ago` : `in ${near}`);
  if (absMs < _MINUTE) return { text: "just now", title };
  if (absMs < _HOUR) return { text: ago(`${Math.round(absMs / _MINUTE)} min`), title };
  if (absMs < _DAY) return { text: ago(`${Math.round(absMs / _HOUR)}h`), title };
  if (absMs < 2 * _DAY) return { text: `${diffMs < 0 ? "yesterday" : "tomorrow"} ${_TIME.format(d)}`, title };
  if (absMs < 7 * _DAY) return { text: ago(`${Math.round(absMs / _DAY)} days`), title };
  const sameYear = d.getFullYear() === new Date(nowMs).getFullYear();
  return { text: (sameYear ? _MONTH_DAY : _MONTH_DAY_YEAR).format(d), title };
}

export function geolocationApprovalTitle(approval: GeolocationApproval): string {
  return approval.mode === "geolocationWatch" ? "Allow continuous location sharing?" : "Allow location sharing?";
}

export function geolocationApprovalBody(approval: GeolocationApproval): string {
  return approval.mode === "geolocationWatch"
    ? "Haku is asking to continuously receive your device location until it stops the watch or you withdraw access."
    : "Haku is asking to read your current device location once.";
}

// Same title/body whether this is the first grant or a resume after the operator stopped
// sharing from their browser's own chrome — approving either way opens the browser's own
// tab-share picker, so the ask reads the same either time.
export const SCREENSHOT_APPROVAL_TITLE = "Allow screen capture for a screenshot?";
export const SCREENSHOT_APPROVAL_BODY =
  "Haku is asking to capture a screenshot of this page. Approving opens your browser's own " +
  "share-this-tab picker; once granted, further screenshots are instant until you withdraw or " +
  "stop sharing.";
